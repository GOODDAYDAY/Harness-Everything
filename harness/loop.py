"""Main harness loop — plan → execute → evaluate → repeat."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from harness.config import HarnessConfig
from harness.evaluator import Evaluator, Verdict
from harness.executor import ExecutionResult, Executor
from harness.llm import LLM
from harness.planner import Planner
from harness.tools import build_registry

log = logging.getLogger(__name__)


@dataclass
class IterationRecord:
    """What happened in one iteration."""

    iteration: int
    plan: str
    result: ExecutionResult
    verdict: Verdict


@dataclass
class HarnessResult:
    """Final output of the harness loop."""

    success: bool
    iterations: list[IterationRecord] = field(default_factory=list)
    final_result: ExecutionResult | None = None

    @property
    def total_tool_calls(self) -> int:
        return sum(len(it.result.log) for it in self.iterations)


class HarnessLoop:
    """Orchestrates the plan → execute → evaluate loop."""

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.llm = LLM(config)
        self.registry = build_registry(config.allowed_tools or None)
        self.planner = Planner(self.llm, config)
        self.executor = Executor(self.llm, self.registry, config)
        self.evaluator = Evaluator(self.llm, config)

    async def run(self, task: str) -> HarnessResult:
        """Run the full loop until the evaluator passes or max iterations hit."""
        context = ""
        iterations: list[IterationRecord] = []

        for i in range(1, self.config.max_iterations + 1):
            log.info("=== Iteration %d/%d ===", i, self.config.max_iterations)

            # 1. Plan
            log.info("Planning...")
            plan = await self.planner.plan(task, context)
            log.info("Plan ready (%d chars)", len(plan))

            # 2. Execute
            log.info("Executing...")
            result = await self.executor.execute(plan, context)
            log.info("Execution done: %d tool calls, %d files changed",
                     len(result.log), len(result.files_changed))

            # 3. Evaluate
            log.info("Evaluating...")
            verdict = await self.evaluator.evaluate(task, plan, result)
            log.info("Verdict: passed=%s reason=%s", verdict.passed, verdict.reason)

            record = IterationRecord(
                iteration=i, plan=plan, result=result, verdict=verdict
            )
            iterations.append(record)

            if verdict.passed:
                log.info("Task completed successfully after %d iteration(s).", i)
                return HarnessResult(
                    success=True, iterations=iterations, final_result=result
                )

            # Feed evaluation feedback into the next iteration
            context += (
                f"\n\n## Iteration {i} Feedback\n\n"
                f"The previous attempt was rejected.\n"
                f"Reason: {verdict.reason}\n"
                f"Feedback: {verdict.feedback}\n"
            )

        log.warning("Max iterations (%d) reached without passing.", self.config.max_iterations)
        return HarnessResult(success=False, iterations=iterations)
