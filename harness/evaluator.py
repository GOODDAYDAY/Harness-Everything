"""Evaluator — three-way evaluation of execution results."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from harness.config import HarnessConfig
from harness.executor import ExecutionResult
from harness.llm import LLM
from harness.three_way import ThreeWayResolver
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
        is_error = out.lower().startswith("error") or "[error]" in out.lower()

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
            # Verbose tool — suppress after a threshold
            verbose_count += 1
            if verbose_count > 8:
                verbose_suppressed += 1
                continue
            snippet = out[:_ENTRY_CAP_VERBOSE].replace("\n", " ↵ ")
            lines.append(f"  · {tool}({key}) → {snippet}")

    if suppressed_tail:
        lines.append(f"  … {suppressed_tail} more tool call(s) not shown (total={len(execution_log)})")
    if verbose_suppressed:
        lines.append(f"  … {verbose_suppressed} read/search call(s) collapsed (not shown)")

    return "\n".join(lines)


@dataclass
class Verdict:
    """Evaluation outcome."""

    passed: bool
    reason: str
    feedback: str  # actionable feedback for the next iteration


class Evaluator:
    """Evaluate execution results via three-way resolution."""

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

        Returns a Verdict with pass/fail and feedback.
        """
        user_message = (
            f"## Original Task\n\n{task}\n\n"
            f"## Plan That Was Executed\n\n{plan}\n\n"
            f"## Execution Log\n\n{_build_log_summary(result.log)}\n\n"
            f"## Executor Summary\n\n{result.text}\n\n"
            f"## Files Changed\n\n{', '.join(result.files_changed) or '(none)'}\n\n"
            "Please evaluate whether the task was completed correctly."
        )

        cfg = self.config.evaluator

        three_way = await self.resolver.resolve(
            user_message,
            conservative_system=cfg.conservative_system or default_prompts.CONSERVATIVE_SYSTEM,
            aggressive_system=cfg.aggressive_system or default_prompts.AGGRESSIVE_SYSTEM,
            merge_system=cfg.merge_system or default_prompts.MERGE_SYSTEM,
        )

        # Parse the merged verdict
        merged_upper = three_way.merged.upper()
        passed = "VERDICT: PASS" in merged_upper

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

        log.info("Evaluator: passed=%s reason=%s", passed, reason)

        return Verdict(
            passed=passed,
            reason=reason or "(no reason extracted)",
            feedback=feedback or three_way.merged,
        )


def build_evaluator(
    llm: LLM,
    config: HarnessConfig,
    mode: str = "three_way",
) -> Evaluator:
    """Factory: return the appropriate evaluator based on mode.

    Args:
        mode: ``"three_way"`` for ThreeWayResolver-based evaluation,
              ``"dual_isolated"`` for DualEvaluator (import separately).
    """
    if mode == "dual_isolated":
        from harness.dual_evaluator import DualEvaluator
        return DualEvaluator(llm)  # type: ignore[return-value]
    return Evaluator(llm, config)
