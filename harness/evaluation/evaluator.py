"""Evaluator — three-way evaluation of execution results.

Augmented with *static analysis*: before passing execution results to the
LLM, we run objective code-quality checks (syntax validation, import
resolution, symbol existence, structural regression) on every changed Python
file.  The findings are injected into the evaluation prompt so the LLM verdict
is grounded in facts, not just opinion.

Key behaviour change
--------------------
* If static analysis finds **ERROR** findings (syntax errors, missing
  symbols), the verdict is forced to FAIL immediately — no LLM call needed.
  This prevents the LLM from rationalising away hard compile-time errors.
* **WARN** findings (unknown imports, removed names) are included in the
  prompt as advisory context; the LLM can weigh them appropriately.
* When there are no changed Python files, static analysis is a no-op (empty
  block) and the existing three-way resolution runs unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.core.config import HarnessConfig
from harness.pipeline.executor import ExecutionResult
from harness.core.llm import LLM
from harness.evaluation.static_analysis import StaticReport, run_static_checks
from harness.pipeline.three_way import ThreeWayResolver
from harness.prompts import evaluator as default_prompts

log = logging.getLogger(__name__)

# Tools whose output is usually large and low-signal for the evaluator — we
# keep only a short snippet instead of 200 chars.
_VERBOSE_TOOLS = frozenset({
    "read_file", "batch_read",
    "bash", "tree", "list_directory",
    "grep_search", "glob_search",
})
# Tools whose output is always useful in full (up to the per-entry cap).
_IMPORTANT_TOOLS = frozenset({
    "write_file", "edit_file", "delete_file", "move_file", "copy_file",
    "file_patch",
    "batch_edit", "batch_write",
})
_ENTRY_CAP_IMPORTANT = 300   # chars kept per important tool output
_ENTRY_CAP_VERBOSE = 80      # chars kept per verbose tool output
_MAX_LOG_ENTRIES = 40        # cap total entries shown (avoids 30-turn loops flooding the prompt)
_VERBOSE_SHOW_LIMIT = 8      # max verbose-tool entries shown before collapsing

# Regex that detects tool output lines that represent errors.  Matches
# case-insensitively so "Error:", "ERROR:", "[Error]", "[ERROR]", etc. all
# trigger the error display path.  The previous startswith("error") check
# missed prefixed error strings like "[stderr]\nError: ...".
_ERROR_PATTERN = re.compile(r"(^error\b|\[error\]|^stderr\b)", re.IGNORECASE | re.MULTILINE)


def _is_tool_error(output: str) -> bool:
    """Return True when *output* looks like a tool error response.

    More robust than the previous ``output.lower().startswith("error")``
    check, which missed error strings preceded by a prefix such as
    ``"[stderr]\\nError: ..."`` and failed on ``"ERROR:"`` (exact case).
    """
    return bool(_ERROR_PATTERN.search(output[:500]))


def _build_log_summary(execution_log: list[dict]) -> str:
    """Build a compact, signal-dense execution log for the evaluator.

    Strategy:
    - File-mutating tool calls (write, edit, delete, move, copy, patch) are shown
      with up to _ENTRY_CAP_IMPORTANT chars of output — these are what the
      evaluator most needs to verify.
    - Read/search/bash calls are collapsed to a one-liner with a short snippet so
      the evaluator can see what was inspected without drowning in file contents.
    - When the log exceeds _MAX_LOG_ENTRIES, excess read/search entries are
      replaced with a count summary to keep the prompt tight.
    - Error results are always shown in full (up to 400 chars) regardless of
      tool type, because errors are high-signal for the evaluator.
    """
    if not execution_log:
        return "(no tool calls)"

    lines: list[str] = []
    verbose_count = 0
    verbose_suppressed = 0

    shown = execution_log[:_MAX_LOG_ENTRIES]
    suppressed_tail = len(execution_log) - len(shown)

    for entry in shown:
        tool = entry["tool"]
        inp = entry.get("input", {})
        out = str(entry.get("output", ""))
        is_error = _is_tool_error(out)

        # Build a concise key for the input
        key = (
            inp.get("path")
            or inp.get("source")
            or inp.get("destination")
            or (f"$ {inp['command'][:60]}" if "command" in inp else None)
            or (f"pattern={inp['pattern']!r}" if "pattern" in inp else None)
            or ""
        )

        if is_error:
            snippet = out[:400]
            lines.append(f"  ✗ {tool}({key}) → {snippet}")
        elif tool in _IMPORTANT_TOOLS:
            snippet = out[:_ENTRY_CAP_IMPORTANT].replace("\n", " ↵ ")
            lines.append(f"  ✓ {tool}({key}) → {snippet}")
        else:
            # Verbose tool — suppress after threshold.
            # Use >= so that entries 1..LIMIT are shown and LIMIT+1 onward
            # are collapsed.  The previous > check showed entries 1..LIMIT+1
            # (one more than intended) before starting to suppress.
            if verbose_count >= _VERBOSE_SHOW_LIMIT:
                verbose_suppressed += 1
                continue
            verbose_count += 1
            snippet = out[:_ENTRY_CAP_VERBOSE].replace("\n", " ↵ ")
            lines.append(f"  · {tool}({key}) → {snippet}")

    if suppressed_tail:
        lines.append(f"  … {suppressed_tail} more tool call(s) not shown (total={len(execution_log)})")
    if verbose_suppressed:
        lines.append(f"  … {verbose_suppressed} read/search call(s) collapsed (not shown)")

    return "\n".join(lines)


def _extract_before_snapshots(execution_log: list[dict]) -> dict[str, str]:
    """Extract pre-execution file content from read_file calls in the log.

    The executor is instructed to ``read_file`` every file before editing it.
    We harvest those reads to build a ``{rel_path: source_before}`` snapshot
    dictionary that enables the structural-regression check in static analysis.

    Only the *first* read of each path is kept (subsequent reads may be
    post-edit verification reads, which would give us the *after* state).

    Args:
        execution_log: The tool call log from ``ExecutionResult.log``.

    Returns:
        A mapping of file path → content-before-execution.
    """
    snapshots: dict[str, str] = {}
    for entry in execution_log:
        if entry.get("tool") != "read_file":
            continue
        path = entry.get("input", {}).get("path", "")
        output = entry.get("output", "")
        if not path or not output:
            continue
        # Normalise path separators and skip already-seen paths
        norm = str(Path(path))
        if norm in snapshots:
            continue  # keep first (pre-edit) read only
        # Strip the line-number header emitted by ReadFileTool:
        #   "[filename.py] lines 1-N of M\n     1\tline content..."
        # We want raw source text for AST parsing, not the annotated form.
        lines = output.split("\n")
        # The header is always the first line when produced by ReadFileTool
        if lines and lines[0].startswith("[") and "] lines " in lines[0]:
            raw_lines: list[str] = []
            for ln in lines[1:]:
                # Each content line is: "   N\t<actual code line>"
                tab_pos = ln.find("\t")
                if tab_pos >= 0:
                    raw_lines.append(ln[tab_pos + 1:])
                else:
                    raw_lines.append(ln)
            snapshots[norm] = "\n".join(raw_lines)
        else:
            snapshots[norm] = output
    return snapshots


@dataclass
class Verdict:
    """Evaluation outcome with structured scoring."""
    
    passed: bool
    reason: str
    feedback: str  # actionable feedback for the next iteration
    static_report: StaticReport | None = None
    
    # Structured scoring fields
    score: float = 0.0  # 0-10 scale
    score_breakdown: dict[str, float] = field(default_factory=dict)  # dimension → score
    top_defect: str = ""  # Most critical issue
    actionable_items: list[str] = field(default_factory=list)  # Specific action items
    evaluation_mode: str = "implementation"  # "implementation" or "phase"  # attached for downstream use
    score_confidence: float = 0.0  # 0-1 confidence in score calibration
    critique_structure_score: float = 0.0  # 0-1 rating of critique structure quality
    calibration_anchors_used: bool = False  # Whether calibration anchors were detected
    validation_warnings: list[str] = field(default_factory=list)  # Validation warnings from output


class Evaluator:
    """Evaluate execution results via static analysis + three-way resolution.

    Static analysis runs first and provides objective findings (syntax errors,
    missing imported symbols, structural regressions).  These findings are
    injected into the LLM evaluation prompt so the reviewer is anchored to
    facts rather than guessing.

    Auto-fail rule: when the static analysis report contains ERROR findings,
    the verdict is forced to FAIL without making an LLM call.  This prevents
    the LLM from rationalising away hard compile-time errors and saves tokens.
    """

    def __init__(self, llm: LLM, config: HarnessConfig) -> None:
        self.llm = llm
        self.config = config
        self.resolver = ThreeWayResolver(llm)

    async def evaluate(
        self,
        task: str,
        plan: str,
        result: ExecutionResult,
    ) -> Verdict:
        """Evaluate whether the execution fulfilled the task.

        Steps:
        1. Run static analysis on all changed Python files.
        2. If there are ERROR findings → force FAIL immediately.
        3. Inject the static report into the LLM prompt.
        4. Run three-way resolution as before.
        5. Return a Verdict with the static report attached.

        Returns a Verdict with pass/fail, reason, feedback, and the
        StaticReport so callers can surface objective findings if needed.
        """
        # Step 1: static analysis
        before_snapshots = _extract_before_snapshots(result.log)
        static_report = run_static_checks(
            result.files_changed,
            self.config.workspace,
            before_snapshots=before_snapshots,
        )

        # Step 2: auto-fail on objective errors (syntax, missing symbols)
        if static_report.has_errors:
            error_summary = "\n".join(
                f"  [{f.file}:{f.line or '?'}] {f.message}"
                for f in static_report.errors
            )
            reason = (
                f"Static analysis found {len(static_report.errors)} error(s) "
                f"that prevent execution: {static_report.errors[0].message[:120]}"
            )
            feedback = (
                "## Static Analysis Errors (fix these first)\n\n"
                f"{error_summary}\n\n"
                "The above errors are objective — they are not LLM opinions.\n"
                "Fix all ERROR findings before attempting other improvements.\n\n"
                f"Full static analysis report:\n\n{static_report.to_prompt_block()}"
            )
            log.warning(
                "Evaluator: auto-FAIL due to %d static error(s): %s",
                len(static_report.errors),
                "; ".join(f.message[:80] for f in static_report.errors[:3]),
            )
            return Verdict(
                passed=False,
                reason=reason,
                feedback=feedback,
                static_report=static_report,
            )

        # Step 3: build LLM evaluation message, prepending static analysis block
        static_block = static_report.to_prompt_block()
        user_message_parts: list[str] = []

        if static_block:
            user_message_parts.append(static_block)
            user_message_parts.append("")  # blank line separator

        # Include executor STATUS field so reviewers can distinguish DONE vs
        # PARTIAL without re-parsing the full summary text.
        executor_status = _extract_executor_status(result.text)
        executor_summary_block = result.text
        if executor_status:
            executor_summary_block = (
                f"**Executor STATUS: {executor_status}**\n\n{result.text}"
            )

        user_message_parts += [
            f"## Original Task\n\n{task}",
            f"## Plan That Was Executed\n\n{plan}",
            f"## Execution Log\n\n{_build_log_summary(result.log)}",
            f"## Executor Summary\n\n{executor_summary_block}",
            f"## Files Changed\n\n{', '.join(result.files_changed) or '(none)'}",
            "Please evaluate whether the task was completed correctly.",
        ]

        user_message = "\n\n".join(user_message_parts)

        cfg = self.config.evaluator

        # Step 4: three-way resolution
        three_way = await self.resolver.resolve(
            user_message,
            conservative_system=cfg.conservative_system or default_prompts.CONSERVATIVE_SYSTEM,
            aggressive_system=cfg.aggressive_system or default_prompts.AGGRESSIVE_SYSTEM,
            merge_system=cfg.merge_system or default_prompts.MERGE_SYSTEM,
        )

        # Parse the merged verdict.
        # Special case: if the executor reported STATUS: PARTIAL, treat the
        # verdict as FAIL regardless of what the LLM said — a partial execution
        # cannot pass by definition.  This prevents a lenient merger from
        # passing an admittedly incomplete execution.
        merged_upper = three_way.merged.upper()
        passed = "VERDICT: PASS" in merged_upper
        if executor_status == "PARTIAL" and passed:
            log.info(
                "Evaluator: overriding PASS → FAIL because executor STATUS=PARTIAL"
            )
            passed = False

        reason = ""
        feedback = ""
        lines = three_way.merged.split("\n")

        # Extract REASON (single-line)
        for line in lines:
            if line.strip().upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
                break

        # Extract FEEDBACK — supports both:
        #   single-line: "FEEDBACK: some text"
        #   multi-line:  "FEEDBACK:\nline1\nline2\nEND_FEEDBACK"
        feedback_lines: list[str] = []
        in_feedback = False
        for line in lines:
            stripped_upper = line.strip().upper()
            if stripped_upper == "END_FEEDBACK":
                break
            if in_feedback:
                feedback_lines.append(line)
            elif stripped_upper.startswith("FEEDBACK:"):
                rest = line.split(":", 1)[1].strip()
                if rest:
                    # Single-line form: "FEEDBACK: text on same line"
                    feedback_lines.append(rest)
                in_feedback = True
        feedback = "\n".join(feedback_lines).strip()

        # When the executor was PARTIAL, prepend a clear note so the next
        # planner iteration focuses on completing the skipped steps.
        if executor_status == "PARTIAL":
            partial_note = (
                "## Execution Was Incomplete (STATUS: PARTIAL)\n\n"
                "The executor reported that some plan steps were skipped or "
                "encountered unresolved issues.  The next iteration must:\n"
                "1. Re-read the ISSUES field in the executor summary above.\n"
                "2. Complete every skipped step before attempting new changes.\n\n"
            )
            feedback = partial_note + feedback if feedback else partial_note + three_way.merged

        # Append static warnings to feedback so the next iteration sees them
        # even when the LLM verdict is PASS.
        if static_report.warnings:
            warn_lines = "\n".join(
                f"  [{f.file}:{f.line or '?'}] {f.message}"
                for f in static_report.warnings
            )
            warning_appendix = (
                "\n\n## Static Analysis Warnings (advisory — not blocking)\n\n"
                f"{warn_lines}"
            )
            feedback = (feedback + warning_appendix) if feedback else warning_appendix

        log.info(
            "Evaluator: passed=%s reason=%s  static=(%d err, %d warn)  executor_status=%s",
            passed, reason, len(static_report.errors), len(static_report.warnings),
            executor_status or "DONE",
        )

        # Extract structured feedback and validate output
        structured_feedback = _extract_structured_feedback(three_way.merged)
        is_valid, validation_warnings = _validate_evaluator_output(three_way.merged)
        
        # Log validation warnings
        if validation_warnings:
            log.warning("Evaluator output validation warnings: %s", validation_warnings)
        
        # Extract score from structured feedback or fallback
        score = structured_feedback.get("score")
        if score is None:
            score = _extract_score_from_verdict(three_way.merged)
        
        # Determine evaluation mode based on context
        evaluation_mode = "implementation"
        if "phase" in task.lower() or "phase" in plan.lower():
            evaluation_mode = "phase"
        
        return Verdict(
            passed=passed,
            reason=reason or structured_feedback.get("top_defect") or "(no reason extracted)",
            feedback=feedback or three_way.merged,
            static_report=static_report,
            score=score,
            score_breakdown=structured_feedback.get("score_breakdown", {}),
            top_defect=structured_feedback.get("top_defect", ""),
            actionable_items=structured_feedback.get("actionable_items", []),
            evaluation_mode=evaluation_mode,
            score_confidence=structured_feedback.get("score_confidence", 0.0),
            critique_structure_score=structured_feedback.get("critique_structure_score", 0.0),
            calibration_anchors_used=structured_feedback.get("calibration_anchors_used", False),
            validation_warnings=validation_warnings,
        )


def _extract_executor_status(summary_text: str) -> str:
    """Extract the STATUS field from the executor's summary text.

    Returns ``"DONE"``, ``"PARTIAL"``, or ``""`` (if not found).
    The executor is instructed to write ``STATUS: DONE`` or ``STATUS: PARTIAL``
    as the last labelled field in its summary.
    """
    for line in summary_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("STATUS:"):
            value = stripped.split(":", 1)[1].strip().upper()
            if value in ("DONE", "PARTIAL"):
                return value
    return ""


def _extract_structured_feedback(verdict_text: str, phase_mode: str = "implement") -> dict[str, Any]:
    """Extract structured feedback from evaluator verdict text.
    
    Args:
        verdict_text: Evaluator output text to parse
        phase_mode: Phase mode ("debate" or "implement") to adjust scoring weights
    
    Returns a dict with keys:
        - "score": float or None
        - "score_breakdown": dict mapping dimension names to scores
        - "top_defect": str or None
        - "actionable_items": list of actionable feedback strings
        - "score_confidence": float 0-1 based on calibration anchors and structure
        - "calibration_anchors_used": bool indicating if calibration anchors were detected
        - "critique_structure_score": float 0-1 rating of critique structure quality
        - "validation_warnings": list of validation warnings
        - "phase_mode_adapted": bool indicating if scoring was adjusted for phase mode
        - "calibration_anchor_details": list of specific anchors detected
    """
    import re
    from typing import Any
    
    result: dict[str, Any] = {
        "score": None,
        "score_breakdown": {},
        "top_defect": None,
        "actionable_items": [],
        "score_confidence": 0.0,
        "calibration_anchors_used": False,
        "critique_structure_score": 0.0,
        "validation_warnings": [],
        "phase_mode_adapted": False,
        "calibration_anchor_details": [],
    }
    
    # Enhanced calibration anchor detection with specific anchor types
    calibration_anchors = {
        "scale_definition": [
            r"0-10\s+scale",
            r"scoring\s+calibration",
            r"score\s+range",
        ],
        "low_score_anchor": [
            r"score\s*≤\s*5",
            r"score\s*<\s*5",
            r"critical\s+failure",
            r"unacceptable",
            r"fails\s+core\s+requirement",
        ],
        "mid_score_anchor": [
            r"score\s*=\s*[567]",
            r"partial\s+success",
            r"mixed\s+results",
            r"some\s+issues",
        ],
        "high_score_anchor": [
            r"score\s*≥\s*8",
            r"score\s*>\s*7",
            r"excellent",
            r"outstanding",
            r"perfect\s*—?\s*no\s+issues",
        ],
        "specific_anchors": [
            r"0/10\s*[:—]",
            r"3/10\s*[:—]",
            r"5/10\s*[:—]",
            r"7/10\s*[:—]",
            r"10/10\s*[:—]",
        ]
    }
    
    # Detect calibration anchors
    detected_anchors = []
    for anchor_type, patterns in calibration_anchors.items():
        for pattern in patterns:
            if re.search(pattern, verdict_text, re.IGNORECASE):
                detected_anchors.append(anchor_type)
                break
    
    result["calibration_anchor_details"] = detected_anchors
    result["calibration_anchors_used"] = len(detected_anchors) >= 3  # Require at least 3 anchor types
    
    # Enhanced score extraction with more patterns and validation
    score_patterns = [
        # Strict anchored patterns (highest confidence)
        r"^SCORE:\s*(\d+(?:\.\d+)?)\s*$",
        r"^FINAL\s+SCORE:\s*(\d+(?:\.\d+)?)\s*$",
        r"^COMBINED_SCORE:\s*(\d+(?:\.\d+)?)\s*$",
        # Section patterns
        r"SCORE:\s*(\d+(?:\.\d+)?)(?:\s*$|\s*[—\-])",
        r"FINAL\s+SCORE:\s*(\d+(?:\.\d+)?)(?:\s*$|\s*[—\-])",
        r"COMBINED_SCORE:\s*(\d+(?:\.\d+)?)(?:\s*$|\s*[—\-])",
        # Fallback patterns
        r"Score:\s*(\d+(?:\.\d+)?)",
        r"score.*?(\d+(?:\.\d+)?)",
    ]
    
    all_matches = []
    for pattern in score_patterns:
        matches = re.findall(pattern, verdict_text, re.IGNORECASE | re.MULTILINE)
        all_matches.extend(matches)
    
    if all_matches:
        # Prefer the last match (most likely to be the final score)
        try:
            score = float(all_matches[-1])
            # Validate score is within reasonable range
            if 0 <= score <= 10:
                result["score"] = score
            else:
                result["validation_warnings"].append(f"Score out of range (0-10): {score}")
        except (ValueError, TypeError):
            result["validation_warnings"].append(f"Failed to parse score from: {all_matches[-1]}")
    
    # Enhanced dimension score extraction with phase-mode awareness
    dimension_sections = []
    
    # Look for DETAILS section
    if "DETAILS:" in verdict_text:
        details_match = re.search(r"DETAILS:(.*?)(?:\n\n|\Z)", verdict_text, re.DOTALL | re.IGNORECASE)
        if details_match:
            dimension_sections.append(details_match.group(1))
    
    # Look for dimension breakdown sections
    dimension_pattern = r"(?:DIMENSION|CRITERIA|METRIC)[^:]*:(.*?)(?:\n\n|\Z)"
    dimension_matches = re.findall(dimension_pattern, verdict_text, re.DOTALL | re.IGNORECASE)
    dimension_sections.extend(dimension_matches)
    
    for section in dimension_sections:
        # Enhanced dimension patterns
        dimension_patterns = [
            # Numbered format: "1. Completeness: SCORE: 8 — finding"
            r"(\d+)\.\s+([^:]+):\s+SCORE:\s*(\d+(?:\.\d+)?)\s*[—\-]\s*(.+)",
            # Unnumbered format: "Completeness: SCORE: 8 — finding"
            r"([^:]+):\s+SCORE:\s*(\d+(?:\.\d+)?)\s*[—\-]\s*(.+)",
            # Simple format: "Completeness: 8/10"
            r"([^:]+):\s*(\d+(?:\.\d+)?)\s*/\s*10",
            # Bare format: "Completeness: 8"
            r"([^:]+):\s*(\d+(?:\.\d+)?)",
        ]
        
        for pattern in dimension_patterns:
            matches = re.findall(pattern, section, re.IGNORECASE)
            for match in matches:
                if len(match) == 4:  # Numbered format with finding
                    _, dimension, score, _ = match
                    result["score_breakdown"][dimension.strip()] = float(score)
                elif len(match) == 3:  # Unnumbered format with finding
                    dimension, score, _ = match
                    result["score_breakdown"][dimension.strip()] = float(score)
                elif len(match) == 2:  # Simple or bare format
                    dimension, score = match
                    result["score_breakdown"][dimension.strip()] = float(score)
    
    # Phase-mode adaptation: adjust dimension weights based on mode
    if phase_mode == "debate":
        # In debate mode, emphasize plan quality, reasoning, and completeness
        debate_dimensions = {"plan_quality", "reasoning", "completeness", "feasibility"}
        for dim in debate_dimensions:
            if dim in result["score_breakdown"]:
                result["phase_mode_adapted"] = True
    elif phase_mode == "implement":
        # In implement mode, emphasize correctness, completeness, and code quality
        implement_dimensions = {"correctness", "completeness", "code_quality", "testing"}
        for dim in implement_dimensions:
            if dim in result["score_breakdown"]:
                result["phase_mode_adapted"] = True
    
    # Enhanced top defect extraction
    defect_sources = []
    
    # From REASON section
    if "REASON:" in verdict_text:
        reason_match = re.search(r"REASON:(.*?)(?:\n\n|\Z)", verdict_text, re.DOTALL | re.IGNORECASE)
        if reason_match:
            reason = reason_match.group(1).strip()
            if len(reason) > 10 and not reason.lower().startswith("none"):
                defect_sources.append(("reason_section", reason))
    
    # From defect-specific patterns
    defect_patterns = [
        r"TOP\s+DEFECT[^:]*:(.*?)(?:\n\n|\Z)",
        r"MAIN\s+ISSUE[^:]*:(.*?)(?:\n\n|\Z)",
        r"CRITICAL\s+PROBLEM[^:]*:(.*?)(?:\n\n|\Z)",
    ]
    
    for pattern in defect_patterns:
        matches = re.findall(pattern, verdict_text, re.DOTALL | re.IGNORECASE)
        for match in matches:
            defect = match.strip()
            if len(defect) > 10:
                defect_sources.append(("defect_pattern", defect))
    
    # Select the best defect (prioritize reason section)
    if defect_sources:
        # Prefer reason section defects
        for source_type, defect in defect_sources:
            if source_type == "reason_section":
                result["top_defect"] = defect
                break
        else:
            # Fall back to any defect
            result["top_defect"] = defect_sources[0][1]
    
    # Enhanced actionable items extraction
    suggestions_sections = []
    
    # SUGGESTIONS section
    if "SUGGESTIONS:" in verdict_text:
        suggestions_match = re.search(r"SUGGESTIONS:(.*?)(?:\n\n|\Z)", verdict_text, re.DOTALL | re.IGNORECASE)
        if suggestions_match:
            suggestions_sections.append(suggestions_match.group(1))
    
    # FEEDBACK section
    if "FEEDBACK:" in verdict_text:
        feedback_match = re.search(r"FEEDBACK:(.*?)(?:END_FEEDBACK|\n\n|\Z)", verdict_text, re.DOTALL | re.IGNORECASE)
        if feedback_match:
            suggestions_sections.append(feedback_match.group(1))
    
    # IMPROVEMENTS section
    if "IMPROVEMENTS:" in verdict_text:
        improvements_match = re.search(r"IMPROVEMENTS:(.*?)(?:\n\n|\Z)", verdict_text, re.DOTALL | re.IGNORECASE)
        if improvements_match:
            suggestions_sections.append(improvements_match.group(1))
    
    for section in suggestions_sections:
        # Enhanced item patterns
        item_patterns = [
            # Numbered items: "1. Fix the bug in line 42"
            r"^\s*(\d+)\.\s+(.+)$",
            # Bulleted items: "- Add error handling"
            r"^\s*[-*•]\s+(.+)$",
            # MERGE_SYSTEM format: "<line 42 — fix the bug>"
            r"^\s*<line\s+\d+\s*[—\-]\s*(.+)>$",
            # Action-oriented: "ACTION: Fix the bug"
            r"^\s*ACTION[^:]*:\s*(.+)$",
        ]
        
        for line in section.split('\n'):
            line = line.strip()
            if not line or line.lower() in ["none", "no suggestions", "n/a"]:
                continue
            
            for pattern in item_patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    if len(match.groups()) == 2:
                        _, item_text = match.groups()
                    else:
                        item_text = match.group(1)
                    
                    # Validate actionable item
                    if len(item_text) > 5 and not item_text.lower().startswith("none"):
                        result["actionable_items"].append(item_text.strip())
                    break
    
    # Enhanced score confidence calculation
    confidence_factors = []
    
    # Score present
    if result["score"] is not None:
        confidence_factors.append(("score_present", 0.25))
    
    # Score breakdown present
    if result["score_breakdown"]:
        confidence_factors.append(("score_breakdown", 0.20))
    
    # Top defect present
    if result["top_defect"]:
        confidence_factors.append(("top_defect", 0.15))
    
    # Actionable items present
    if result["actionable_items"]:
        confidence_factors.append(("actionable_items", 0.15))
    
    # Calibration anchors used
    if result["calibration_anchors_used"]:
        confidence_factors.append(("calibration_anchors", 0.15))
    
    # Phase mode adaptation
    if result["phase_mode_adapted"]:
        confidence_factors.append(("phase_mode_adapted", 0.10))
    
    # Calculate confidence
    result["score_confidence"] = sum(weight for _, weight in confidence_factors)
    
    # Enhanced critique structure score
    structure_factors = []
    
    # Has required sections
    required_sections = ["VERDICT:", "REASON:", "DETAILS:"]
    present_sections = sum(1 for section in required_sections if section in verdict_text)
    structure_factors.append(("required_sections", present_sections / len(required_sections) * 0.3))
    
    # Has suggestions/feedback section
    if "SUGGESTIONS:" in verdict_text or "FEEDBACK:" in verdict_text:
        structure_factors.append(("suggestions_section", 0.2))
    
    # Has dimension breakdown
    if result["score_breakdown"]:
        structure_factors.append(("dimension_breakdown", 0.2))
    
    # Has calibration anchors
    if result["calibration_anchors_used"]:
        structure_factors.append(("calibration_anchors", 0.2))
    
    # Well-formatted (no obvious formatting issues)
    formatting_issues = 0
    if "  " in verdict_text:  # Double spaces
        formatting_issues += 1
    if "\t" in verdict_text:  # Tabs
        formatting_issues += 1
    if formatting_issues == 0:
        structure_factors.append(("good_formatting", 0.1))
    
    result["critique_structure_score"] = sum(weight for _, weight in structure_factors)
    
    # Cap scores at 1.0
    result["score_confidence"] = min(1.0, result["score_confidence"])
    result["critique_structure_score"] = min(1.0, result["critique_structure_score"])
    
    return result


def _extract_score_from_verdict(verdict_text: str) -> float | None:
    """Extract a numeric score from verdict text when structured feedback fails.
    
    This is a fallback function that tries to extract a score from various
    patterns in the verdict text when the structured feedback extraction
    doesn't find a score.
    
    Args:
        verdict_text: The evaluator output text
        
    Returns:
        Extracted score (0-10) or None if no score found
    """
    import re
    
    # Patterns to search for scores in the verdict text
    score_patterns = [
        # Final score patterns
        r"FINAL\s+SCORE\s*:\s*(\d+(?:\.\d+)?)",
        r"COMBINED_SCORE\s*:\s*(\d+(?:\.\d+)?)",
        r"SCORE\s*:\s*(\d+(?:\.\d+)?)",
        # Score at end of line patterns
        r"(\d+(?:\.\d+)?)\s*/\s*10\b",
        r"(\d+(?:\.\d+)?)\s*out\s*of\s*10\b",
        r"(\d+(?:\.\d+)?)\s*\(?score\)?",
        # Simple numeric patterns that might be scores
        r"\b(\d+(?:\.\d+)?)\b(?=\s*(?:points?|pts?|score))",
    ]
    
    all_matches = []
    for pattern in score_patterns:
        matches = re.findall(pattern, verdict_text, re.IGNORECASE)
        all_matches.extend(matches)
    
    if all_matches:
        # Prefer the last match (most likely to be the final score)
        try:
            score = float(all_matches[-1])
            # Validate score is within reasonable range (0-10)
            if 0 <= score <= 10:
                return score
        except (ValueError, TypeError):
            pass
    
    # If no score found in patterns, try to extract from context
    # Look for lines that might contain scores
    lines = verdict_text.split('\n')
    for line in lines:
        line_lower = line.lower()
        if any(keyword in line_lower for keyword in ['score', 'rating', 'grade']):
            # Try to extract a number from this line
            numbers = re.findall(r'\b(\d+(?:\.\d+)?)\b', line)
            if numbers:
                try:
                    score = float(numbers[-1])
                    if 0 <= score <= 10:
                        return score
                except (ValueError, TypeError):
                    continue
    
    return None


def _validate_evaluator_output(verdict_text: str) -> tuple[bool, list[str]]:
    """Validate evaluator output structure and return (is_valid, warnings).
    
    Args:
        verdict_text: Evaluator output text to validate
    
    Returns:
        Tuple of (is_valid, list_of_warnings)
    """
    warnings = []
    
    # Check for required sections
    required_sections = ["VERDICT:", "REASON:"]
    for section in required_sections:
        if section not in verdict_text:
            warnings.append(f"Missing required section: {section}")
    
    # Check VERDICT value
    if "VERDICT:" in verdict_text:
        verdict_line = [line for line in verdict_text.split('\n') if line.strip().startswith("VERDICT:")]
        if verdict_line:
            verdict_value = verdict_line[0].split("VERDICT:")[1].strip().upper()
            if verdict_value not in ["PASS", "FAIL"]:
                warnings.append(f"Invalid VERDICT value: '{verdict_value}' (must be PASS or FAIL)")
    
    # Check for score
    if "FINAL SCORE:" not in verdict_text and "COMBINED_SCORE:" not in verdict_text:
        warnings.append("Missing score section (FINAL SCORE: or COMBINED_SCORE:)")
    
    # Check for DETAILS section with dimension scores
    if "DETAILS:" in verdict_text:
        details_section = verdict_text.split("DETAILS:")[1].split("\n\n")[0]
        # Check for at least one dimension score
        if "SCORE:" not in details_section:
            warnings.append("DETAILS section should contain dimension SCORE: entries")
    
    # Check for actionable feedback
    if "SUGGESTIONS:" not in verdict_text and "FEEDBACK:" not in verdict_text:
        warnings.append("Missing actionable feedback section (SUGGESTIONS: or FEEDBACK:)")
    
    is_valid = len(warnings) == 0
    return is_valid, warnings

