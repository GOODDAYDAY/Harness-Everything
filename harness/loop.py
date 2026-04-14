"""Main harness loop — plan → execute → evaluate → repeat."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from harness.config import HarnessConfig
from harness.evaluator import Evaluator, Verdict
from harness.executor import ExecutionResult, Executor
from harness.llm import LLM
from harness.planner import Planner
from harness.project_context import ProjectContextBuilder
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
        self._project_ctx_builder = ProjectContextBuilder(config)

    async def run(self, task: str) -> HarnessResult:
        """Run the full loop until the evaluator passes or max iterations hit."""
        iterations: list[IterationRecord] = []

        run_start = time.monotonic()

        # Collect project context once at run start (snapshot of structure +
        # recent git activity) and prepend it to every planner call so that
        # both the conservative and aggressive proposers know what already
        # exists before deciding what to change.
        t0 = time.monotonic()
        project_ctx = await self._project_ctx_builder.build()
        if project_ctx:
            log.info(
                "project_context: %d chars collected  (%.1fs)",
                len(project_ctx),
                time.monotonic() - t0,
            )
        else:
            log.debug("project_context: nothing collected (%.1fs)", time.monotonic() - t0)

        # feedback_ctx accumulates per-iteration evaluator feedback; it is
        # separate from project_ctx so we can prepend project context to every
        # iteration without re-appending it on each pass.
        feedback_ctx = ""

        for i in range(1, self.config.max_iterations + 1):
            iter_start = time.monotonic()
            log.info("── Iteration %d/%d ──────────────────────────────", i, self.config.max_iterations)

            # Compose full context: project snapshot (stable) + feedback (growing)
            full_context = project_ctx
            if feedback_ctx:
                full_context = (full_context + "\n\n" + feedback_ctx) if full_context else feedback_ctx

            # 1. Plan
            t0 = time.monotonic()
            plan = await self.planner.plan(task, full_context)
            log.info(
                "plan: %d chars  (%.1fs)",
                len(plan),
                time.monotonic() - t0,
            )

            # 2. Execute
            t0 = time.monotonic()
            result = await self.executor.execute(plan, full_context)
            log.info(
                "execute: tool_calls=%d files_changed=%d  (%.1fs)",
                len(result.log),
                len(result.files_changed),
                time.monotonic() - t0,
            )
            if result.files_changed:
                log.info("  changed: %s", ", ".join(result.files_changed))

            # 3. Evaluate
            t0 = time.monotonic()
            verdict = await self.evaluator.evaluate(task, plan, result)
            log.info(
                "evaluate: passed=%s  reason=%r  (%.1fs)",
                verdict.passed,
                verdict.reason,
                time.monotonic() - t0,
            )

            record = IterationRecord(
                iteration=i, plan=plan, result=result, verdict=verdict
            )
            iterations.append(record)

            iter_elapsed = time.monotonic() - iter_start
            if verdict.passed:
                log.info(
                    "✓ Task completed after %d iteration(s)  total=%.1fs",
                    i,
                    time.monotonic() - run_start,
                )
                return HarnessResult(
                    success=True, iterations=iterations, final_result=result
                )

            log.info("✗ Iteration %d failed (%.1fs) — feedback: %s", i, iter_elapsed, verdict.feedback[:200])

            # Accumulate evaluator feedback for the next iteration's planner.
            # We keep this separate from project_ctx so the tree/git block is
            # not duplicated on every loop.
            feedback_ctx += (
                f"\n\n## Iteration {i} Feedback\n\n"
                f"The previous attempt was rejected.\n"
                f"Reason: {verdict.reason}\n"
                f"Feedback: {verdict.feedback}\n"
            )

        log.warning(
            "✗ Max iterations (%d) reached without passing  total=%.1fs",
            self.config.max_iterations,
            time.monotonic() - run_start,
        )
        return HarnessResult(success=False, iterations=iterations)
