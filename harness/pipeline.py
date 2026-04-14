"""PipelineLoop — outer rounds orchestrator for the phase pipeline."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from harness.artifacts import ArtifactStore
from harness.checkpoint import CheckpointManager
from harness.config import PipelineConfig
from harness.llm import LLM
from harness.phase import PhaseConfig, PhaseResult
from harness.phase_runner import PhaseRunner
from harness.tools import build_registry
from harness.memory import MemoryStore

log = logging.getLogger(__name__)

# Matches the "**Best**: 7.5" line written by PhaseRunner._write_phase_summary.
_BEST_SCORE_RE = re.compile(r"^\*\*Best\*\*:\s*(\d+(?:\.\d+)?)", re.MULTILINE)


def _read_best_score_from_summary(summary_text: str) -> float | None:
    """Parse the best score written by PhaseRunner into phase_summary.txt.

    Returns the score as a float, or ``None`` if the text is empty or the
    pattern is absent (e.g., the file was never written).
    """
    if not summary_text:
        return None
    m = _BEST_SCORE_RE.search(summary_text)
    return float(m.group(1)) if m else None


@dataclass
class PipelineResult:
    """Final output of the pipeline."""

    success: bool
    rounds_completed: int
    phases_results: list[list[PhaseResult]] = field(default_factory=list)
    final_proposal: str = ""


class PipelineLoop:
    """Orchestrates outer_rounds → phases → inner_rounds → synthesis.

    This is the alternative to ``HarnessLoop`` for complex multi-phase workflows.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        config.harness.apply_log_level()
        log.info(config.harness.startup_banner())
        self.llm = LLM(config.harness)
        self.registry = build_registry(
            config.harness.allowed_tools or None,
            extra_tools=config.harness.extra_tools or None,
        )

        # Artifact store: resume or create new
        existing = ArtifactStore.find_resumable(config.output_dir)
        if existing:
            self.artifacts = existing
            log.info("Resuming run: %s", self.artifacts.run_dir)
        else:
            self.artifacts = ArtifactStore(config.output_dir, config.run_id)
            log.info("New run: %s", self.artifacts.run_dir)

        self.checkpoint = CheckpointManager(self.artifacts)
        self.runner = PhaseRunner(
            self.llm, self.registry, config, self.artifacts, self.checkpoint
        )
        self.memory = MemoryStore(self.artifacts)

    def _build_phases(self) -> list[PhaseConfig]:
        """Build PhaseConfig list from raw config dicts."""
        return [PhaseConfig.from_dict(p) for p in self.config.phases]

    async def run(self) -> PipelineResult:
        """Execute all outer rounds with resume + early stopping."""
        phases = self._build_phases()
        if not phases:
            log.error("No phases configured!")
            return PipelineResult(success=False, rounds_completed=0)

        log.info(
            "Pipeline: %d outer rounds × %d phases × %d inner rounds  [%s]",
            self.config.outer_rounds, len(phases), self.config.inner_rounds,
            self.artifacts.run_dir,
        )

        prior_best: str | None = None
        best_round_score = 0.0
        no_improve_count = 0
        all_round_results: list[list[PhaseResult]] = []
        pipeline_start = time.monotonic()

        for outer in range(self.config.outer_rounds):
            round_start = time.monotonic()
            log.info(
                "── Round %d/%d ──────────────────────────────────────",
                outer + 1, self.config.outer_rounds,
            )

            round_score = 0.0
            try:
                round_results, prior_best, round_score = await self._run_outer_round(
                    outer, phases, prior_best
                )
                all_round_results.append(round_results)
            except Exception as e:
                log.error("Round %d failed: %s", outer + 1, e, exc_info=True)
                all_round_results.append([])

            round_elapsed = time.monotonic() - round_start
            log.info(
                "Round %d complete: score=%.1f  elapsed=%.1fs",
                outer + 1, round_score, round_elapsed,
            )
            log.info(
                "METRIC %s",
                json.dumps({
                    "event": "pipeline_round_complete",
                    "round": outer + 1,
                    "round_score": round(round_score, 2),
                    "phases_run": len(all_round_results[-1]),
                    "elapsed_s": round(round_elapsed, 2),
                }),
            )

            # Write round summary
            self._write_round_summary(outer, all_round_results[-1], prior_best, round_score)

            # Early stopping — evaluated for every round regardless of score.
            # The old guard `round_score > 0` caused zero-scoring rounds (e.g.
            # all phases failed or all score parses returned 0.0) to be silently
            # skipped, so patience never fired when all rounds scored 0 and the
            # full outer_rounds budget was always consumed.
            if self.config.patience > 0:
                if round_score > best_round_score:
                    best_round_score = round_score
                    no_improve_count = 0
                    log.info("  ↑ New best score: %.1f", best_round_score)
                else:
                    no_improve_count += 1
                    log.info(
                        "  → No improvement (best=%.1f, no_improve=%d/%d)",
                        best_round_score, no_improve_count, self.config.patience,
                    )

                if no_improve_count >= self.config.patience:
                    log.info(
                        "Early stop: no improvement for %d consecutive round(s) (best=%.1f)",
                        no_improve_count, best_round_score,
                    )
                    break

        total_elapsed = time.monotonic() - pipeline_start

        # Final summary
        self.artifacts.write_final_summary(
            f"# Pipeline Complete\n\n"
            f"Rounds: {len(all_round_results)}\n"
            f"Best score: {best_round_score:.1f}\n"
            f"Elapsed: {total_elapsed:.1f}s\n\n"
            f"## Final Proposal\n\n{prior_best or '(none)'}\n"
        )

        log.info(
            "Pipeline complete: rounds=%d  best=%.1f  total=%.1fs  artifacts=%s",
            len(all_round_results), best_round_score, total_elapsed, self.artifacts.run_dir,
        )

        return PipelineResult(
            success=True,
            rounds_completed=len(all_round_results),
            phases_results=all_round_results,
            final_proposal=prior_best or "",
        )

    async def _run_outer_round(
        self,
        outer: int,
        phases: list[PhaseConfig],
        prior_best: str | None,
    ) -> tuple[list[PhaseResult], str | None, float]:
        """Execute all phases for one outer round."""
        results: list[PhaseResult] = []
        phase_scores: list[float] = []

        for phase in phases:
            # Skip logic
            if phase.should_skip(outer):
                if not self.checkpoint.is_phase_skipped(outer, phase.label):
                    self.checkpoint.mark_phase_skipped(outer, phase.label)
                log.info("  phase=%s  status=skipped (skip_after_round=%s)", phase.label, phase.skip_after_round)
                continue

            # Resume: skip completed phases
            if self.checkpoint.is_phase_done(outer, phase.label):
                log.info("  phase=%s  status=resumed (already done)", phase.label)
                segs = self.artifacts.phase_dir(outer, phase.label)
                prior_best = self.artifacts.read(*segs, "synthesis.txt") or prior_best
                # Recover the persisted best_score so that resumed rounds are not
                # misreported as scoring 0.0 — which would cause the patience
                # counter to fire falsely and abort remaining outer rounds.
                resumed_score = _read_best_score_from_summary(
                    self.artifacts.read(*segs, "phase_summary.txt")
                )
                if resumed_score is not None:
                    phase_scores.append(resumed_score)
                    log.info("    ↳ recovered score=%.1f", resumed_score)
                continue

            phase_start = time.monotonic()
            try:
                # Inject memory context from prior rounds into the carry-forward
                # text so the LLM can build on accumulated learnings.
                memory_ctx = self.memory.format_context(phase.label, max_entries=8)
                phase_prior = prior_best
                if memory_ctx:
                    phase_prior = (
                        memory_ctx
                        + ("\n\n" + prior_best if prior_best else "")
                    )

                phase_result = await self.runner.run_phase(outer, phase, phase_prior)
                results.append(phase_result)
                phase_scores.append(phase_result.best_score)
                prior_best = phase_result.synthesis or prior_best
                # Record learnings for future rounds
                self.memory.record(outer, phase_result)
                _phase_elapsed = time.monotonic() - phase_start
                log.info(
                    "  phase=%s  status=done  score=%.1f  elapsed=%.1fs  memory_entries=%d",
                    phase.label, phase_result.best_score, _phase_elapsed,
                    self.memory.entry_count,
                )
                log.info(
                    "METRIC %s",
                    json.dumps({
                        "event": "pipeline_phase_complete",
                        "outer": outer + 1,
                        "phase": phase.label,
                        "best_score": round(phase_result.best_score, 2),
                        "inner_results": len(phase_result.inner_results),
                        "elapsed_s": round(_phase_elapsed, 2),
                    }),
                )
            except Exception as e:
                log.error(
                    "  phase=%s  status=FAILED  elapsed=%.1fs  error=%s",
                    phase.label, time.monotonic() - phase_start, e,
                    exc_info=True,
                )

        round_score = max(phase_scores) if phase_scores else 0.0
        return results, prior_best, round_score

    def _write_round_summary(
        self,
        outer: int,
        results: list[PhaseResult],
        best_proposal: str | None,
        round_score: float,
    ) -> None:
        phase_lines = "\n".join(
            f"- {r.phase.label}: best={r.best_score:.1f}" for r in results
        )
        content = (
            f"# Round {outer + 1} Summary\n\n"
            f"**Score**: {round_score:.1f}\n\n"
            f"## Phases\n\n{phase_lines or '(none completed)'}\n\n"
            f"## Best Proposal\n\n{best_proposal or '(none)'}\n"
        )
        self.artifacts.write(content, f"round_{outer + 1}", "summary.md")
