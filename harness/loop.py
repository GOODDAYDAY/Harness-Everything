"""Main harness loop — plan → execute → evaluate → repeat."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from harness.core.config import HarnessConfig
from harness.evaluation.evaluator import Evaluator, Verdict
from harness.executor import ExecutionResult, Executor
from harness.core.llm import LLM
from harness.planner import Planner
from harness.project_context import ProjectContextBuilder
from harness.tools import build_registry

log = logging.getLogger(__name__)


def _trim_feedback_ctx(ctx: str, cap: int) -> str:
    """Trim feedback context to at most *cap* chars, preserving section boundaries.

    Trims from the oldest end so recent feedback is always retained.  After
    slicing, re-aligns to the start of the next ``## Iteration`` heading so we
    never send a partial feedback block to the planner.  Falls back to the raw
    slice when no heading boundary is found (rare: single very long iteration).

    Unlike the previous inline approach this is testable and logs at DEBUG
    rather than polluting the INFO stream on every iteration.
    """
    if len(ctx) <= cap:
        return ctx
    trimmed = ctx[-cap:]
    # Re-align to the start of a section heading
    boundary = trimmed.find("\n\n## Iteration")
    if boundary > 0:
        trimmed = trimmed[boundary:]
    log.debug(
        "feedback_ctx trimmed: %d → %d chars (cap=%d, boundary_found=%s)",
        len(ctx), len(trimmed), cap, boundary > 0,
    )
    return trimmed


def _format_iteration_feedback(
    i: int,
    verdict: "Verdict",
    result: "ExecutionResult",
) -> str:
    """Format one iteration's feedback as a compact, signal-dense block.

    Compared to the previous free-form string, this format:
    * Leads with a machine-scannable header line the planner can orient by.
    * Includes tool-call count and files-changed count as numeric signals so
      the planner knows whether the executor was busy or idle.
    * Includes the static-error count so the planner prioritises compile
      fixes over logic fixes when both are present.
    * Caps the feedback body at 3 000 chars so a single verbose iteration
      cannot dominate the whole context window.

    The ``## Iteration N Feedback`` heading is preserved so
    ``_trim_feedback_ctx`` can re-align on section boundaries.
    """
    static_errs = len(verdict.static_report.errors) if verdict.static_report else 0
    static_warns = len(verdict.static_report.warnings) if verdict.static_report else 0
    files_changed = len(result.files_changed)
    tool_calls = len(result.log)

    header = (
        f"\n\n## Iteration {i} Feedback\n"
        f"<!-- stats: tool_calls={tool_calls} files_changed={files_changed} "
        f"static_errors={static_errs} static_warnings={static_warns} -->\n"
    )
    body = (
        f"**Result:** FAIL\n"
        f"**Reason:** {verdict.reason}\n"
    )
    if static_errs:
        body += f"**Static errors ({static_errs}):** fix these before anything else.\n"
    feedback_body = verdict.feedback[:3_000]
    if len(verdict.feedback) > 3_000:
        feedback_body += "\n… (feedback truncated)"
    body += f"\n**Feedback:**\n{feedback_body}\n"
    return header + body


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
        config.apply_log_level()
        log.info(config.startup_banner())
        self.llm = LLM(config)
        self.registry = build_registry(
            config.allowed_tools or None,
            extra_tools=config.extra_tools or None,
        )
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
        # Cap: keep only the most recent _FEEDBACK_CTX_CHARS chars so that a
        # long-running loop (many iterations, verbose feedback) doesn't crowd
        # the actual task description and plan out of the context window.
        feedback_ctx = ""
        _FEEDBACK_CTX_CHARS = 8_000  # ~2 000 tokens — enough for 3-4 rich feedbacks

        for i in range(1, self.config.max_iterations + 1):
            iter_start = time.monotonic()
            log.info("── Iteration %d/%d ──────────────────────────────", i, self.config.max_iterations)

            # Compose full context: project snapshot (stable) + feedback (growing)
            full_context = project_ctx
            if feedback_ctx:
                full_context = (full_context + "\n\n" + feedback_ctx) if full_context else feedback_ctx
            log.debug(
                "context_budget: project=%d feedback=%d total=%d chars",
                len(project_ctx), len(feedback_ctx), len(full_context),
            )

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
            _exec_elapsed = time.monotonic() - t0
            log.info(
                "execute: tool_calls=%d files_changed=%d  (%.1fs)",
                len(result.log),
                len(result.files_changed),
                _exec_elapsed,
            )
            if result.files_changed:
                log.info("  changed: %s", ", ".join(result.files_changed))

            # 3. Evaluate
            t0 = time.monotonic()
            verdict = await self.evaluator.evaluate(task, plan, result)
            _eval_elapsed = time.monotonic() - t0
            log.info(
                "evaluate: passed=%s  reason=%r  (%.1fs)",
                verdict.passed,
                verdict.reason,
                _eval_elapsed,
            )
            log.info(
                "METRIC %s",
                json.dumps({
                    "event": "harness_iteration",
                    "iteration": i,
                    "passed": verdict.passed,
                    "tool_calls": len(result.log),
                    "files_changed": len(result.files_changed),
                    "exec_elapsed_s": round(_exec_elapsed, 2),
                    "eval_elapsed_s": round(_eval_elapsed, 2),
                }),
            )

            record = IterationRecord(
                iteration=i, plan=plan, result=result, verdict=verdict
            )
            iterations.append(record)

            iter_elapsed = time.monotonic() - iter_start
            if verdict.passed:
                total_elapsed = time.monotonic() - run_start
                log.info(
                    "✓ Task completed after %d iteration(s)  total=%.1fs",
                    i,
                    total_elapsed,
                )
                log.info(
                    "METRIC %s",
                    json.dumps({
                        "event": "harness_run_complete",
                        "success": True,
                        "iterations": i,
                        "total_tool_calls": sum(len(it.result.log) for it in iterations),
                        "elapsed_s": round(total_elapsed, 2),
                    }),
                )
                return HarnessResult(
                    success=True, iterations=iterations, final_result=result
                )

            log.info("✗ Iteration %d failed (%.1fs) — feedback: %s", i, iter_elapsed, verdict.feedback[:200])

            # Accumulate evaluator feedback for the next iteration's planner.
            # We keep this separate from project_ctx so the tree/git block is
            # not duplicated on every loop.
            new_feedback = _format_iteration_feedback(i, verdict, result)
            feedback_ctx += new_feedback
            feedback_ctx = _trim_feedback_ctx(feedback_ctx, _FEEDBACK_CTX_CHARS)

        total_elapsed = time.monotonic() - run_start
        log.warning(
            "✗ Max iterations (%d) reached without passing  total=%.1fs",
            self.config.max_iterations,
            total_elapsed,
        )
        # Emit the completion metric even on failure so dashboards see a
        # consistent event for every run regardless of outcome.
        log.info(
            "METRIC %s",
            json.dumps({
                "event": "harness_run_complete",
                "success": False,
                "iterations": self.config.max_iterations,
                "total_tool_calls": sum(len(it.result.log) for it in iterations),
                "elapsed_s": round(total_elapsed, 2),
            }),
        )
        return HarnessResult(success=False, iterations=iterations)
