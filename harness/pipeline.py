"""PipelineLoop — outer rounds orchestrator for the phase pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from harness.artifacts import ArtifactStore
from harness.checkpoint import CheckpointManager
from harness.config import PipelineConfig
from harness.llm import LLM
from harness.phase import PhaseConfig, PhaseResult
from harness.phase_runner import PhaseRunner
from harness.tools import build_registry

log = logging.getLogger(__name__)


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
        self.llm = LLM(config.harness)
        self.registry = build_registry(config.harness.allowed_tools or None)

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
            "Pipeline: %d outer rounds x %d phases x %d inner rounds",
            self.config.outer_rounds, len(phases), self.config.inner_rounds,
        )

        prior_best: str | None = None
        best_round_score = 0.0
        no_improve_count = 0
        all_round_results: list[list[PhaseResult]] = []

        for outer in range(self.config.outer_rounds):
            log.info("=== Outer Round %d/%d ===", outer + 1, self.config.outer_rounds)

            try:
                round_results, prior_best, round_score = await self._run_outer_round(
                    outer, phases, prior_best
                )
                all_round_results.append(round_results)
            except Exception as e:
                log.error("Round %d failed: %s", outer + 1, e)
                round_score = 0.0
                all_round_results.append([])

            # Write round summary
            self._write_round_summary(outer, all_round_results[-1], prior_best, round_score)

            # Early stopping
            if self.config.patience > 0 and round_score > 0:
                if round_score > best_round_score:
                    best_round_score = round_score
                    no_improve_count = 0
                else:
                    no_improve_count += 1

                if no_improve_count >= self.config.patience:
                    log.info(
                        "Early stop: no improvement for %d rounds (best=%.1f)",
                        no_improve_count, best_round_score,
                    )
                    break

        # Final summary
        self.artifacts.write_final_summary(
            f"# Pipeline Complete\n\n"
            f"Rounds: {len(all_round_results)}\n"
            f"Best score: {best_round_score:.1f}\n\n"
            f"## Final Proposal\n\n{prior_best or '(none)'}\n"
        )

        log.info("Pipeline complete. Artifacts: %s", self.artifacts.run_dir)

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
                log.info("Phase %s: skipped (outer=%d)", phase.label, outer + 1)
                continue

            # Resume: skip completed phases
            if self.checkpoint.is_phase_done(outer, phase.label):
                log.info("Phase %s: already done, loading synthesis", phase.label)
                segs = self.artifacts.phase_dir(outer, phase.label)
                prior_best = self.artifacts.read(*segs, "synthesis.txt") or prior_best
                continue

            try:
                phase_result = await self.runner.run_phase(outer, phase, prior_best)
                results.append(phase_result)
                phase_scores.append(phase_result.best_score)
                prior_best = phase_result.synthesis or prior_best
            except Exception as e:
                log.error("Phase %s failed: %s", phase.label, e)

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
