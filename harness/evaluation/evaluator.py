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
from dataclasses import dataclass
from pathlib import Path

from harness.core.config import HarnessConfig
from harness.pipeline.executor import ExecutionResult
from harness.core.llm import LLM
from harness.evaluation.static_analysis import StaticReport, run_static_checks
from harness.pipeline.three_way import ThreeWayResolver
from harness.prompts import evaluator as default_prompts

log = logging.getLogger(__name__)

# Tools whose output is usually large and low-signal for the evaluator — we
# keep only a short snippet instead of 200 chars.
_VERBOSE_TOOLS = frozenset({"read_file", "bash", "tree", "list_directory", "grep_search", "glob_search"})
# Tools whose output is always useful in full (up to the per-entry cap).
_IMPORTANT_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "move_file", "copy_file", "file_patch"})
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

        return Verdict(
            passed=passed,
            reason=reason or "(no reason extracted)",
            feedback=feedback or three_way.merged,
            static_report=static_report,
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


def _extract_score_from_verdict(verdict_text: str) -> float:
    """Extract numeric score from evaluator verdict text.
    
    Looks for patterns like:
    - FINAL SCORE: 8.5
    - COMBINED_SCORE: 7.2/10
    - Score: 9
    
    Returns 0.0 if no score found.
    """
    import re
    
    # Try to find FINAL SCORE: X or COMBINED_SCORE: X
    patterns = [
        r"FINAL\s+SCORE[:\s]+(\d+(?:\.\d+)?)",
        r"COMBINED_SCORE[:\s]+(\d+(?:\.\d+)?)",
        r"Score[:\s]+(\d+(?:\.\d+)?)",
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, verdict_text, re.IGNORECASE)
        if matches:
            try:
                return float(matches[-1])
            except (ValueError, TypeError):
                continue
    
    # Try to extract from individual dimension scores and average them
    dimension_scores = []
    dimension_pattern = r"(\d+)\.\s+\w+:\s+SCORE:\s+(\d+)"
    matches = re.findall(dimension_pattern, verdict_text, re.IGNORECASE)
    for _, score_str in matches:
        try:
            dimension_scores.append(float(score_str))
        except (ValueError, TypeError):
            continue
    
    if dimension_scores:
        return sum(dimension_scores) / len(dimension_scores)
    
    return 0.0

