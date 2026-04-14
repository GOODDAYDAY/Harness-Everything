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
        # Build a comprehensive summary for the evaluator
        log_summary = "\n".join(
            f"- {entry['tool']}({', '.join(f'{k}={v!r}' for k, v in entry['input'].items())})"
            f"\n  → {entry['output'][:200]}"
            for entry in result.log
        )

        user_message = (
            f"## Original Task\n\n{task}\n\n"
            f"## Plan That Was Executed\n\n{plan}\n\n"
            f"## Execution Log\n\n{log_summary}\n\n"
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
        merged = three_way.merged.upper()
        passed = "VERDICT: PASS" in merged

        # Extract feedback
        feedback = ""
        for line in three_way.merged.split("\n"):
            if line.strip().upper().startswith("FEEDBACK:"):
                feedback = line.split(":", 1)[1].strip()
                break

        reason = ""
        for line in three_way.merged.split("\n"):
            if line.strip().upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
                break

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
