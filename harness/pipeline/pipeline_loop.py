"""PipelineLoop — outer rounds orchestrator for the phase pipeline."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.artifacts import ArtifactStore
from harness.checkpoint import CheckpointManager
from harness.core.config import PipelineConfig
from harness.core.llm import LLM
from harness.memory import MemoryStore
from harness.metrics import MetricsCollector
from harness.pipeline.phase import PhaseConfig, PhaseResult
from harness.pipeline.phase_runner import PhaseRunner
from harness.tools import build_registry

log = logging.getLogger(__name__)

# Number of consecutive declining round scores that triggers an early-stop
# warning and is logged for operators.
_DECLINE_WARN_STREAK: int = 3

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
        self._metrics_collector = MetricsCollector(
            output_path=Path(self.config.harness.workspace) / ".harness_metrics.json"
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
            "Pipeline: %d outer rounds × %d phases × %d inner rounds  [%s]",
            self.config.outer_rounds, len(phases), self.config.inner_rounds,
            self.artifacts.run_dir,
        )

        prior_best: str | None = None
        best_round_score = 0.0
        no_improve_count = 0
        all_round_results: list[list[PhaseResult]] = []
        pipeline_start = time.monotonic()
        # score_history: (round_index_1based, score) — used for trend detection
        # and the run-level summary.json
        score_history: list[dict] = []
        # Consecutive-decline tracking for trend detection (Priority 4)
        _decline_streak: int = 0
        _prev_score: float | None = None
        # Run-level tool call counters for summary.json tool_error_rate
        _total_tool_calls: int = 0
        _total_tool_errors: int = 0

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

            score_history.append({"round": outer + 1, "score": round(round_score, 2)})

            # --- Priority 4: Score trend detection ---
            # Detect 3 consecutive declining scores and log a warning.
            if _prev_score is not None:
                if round_score < _prev_score:
                    _decline_streak += 1
                    if _decline_streak >= _DECLINE_WARN_STREAK:
                        log.warning(
                            "TREND WARNING: score has declined for %d consecutive "
                            "round(s) (%.1f → %.1f → … → %.1f). "
                            "Consider adjusting the prompt or stopping early.",
                            _decline_streak,
                            score_history[-_decline_streak]["score"],
                            score_history[-1]["score"],
                            round_score,
                        )
                else:
                    _decline_streak = 0
            _prev_score = round_score

            # Write round summary
            self._write_round_summary(outer, all_round_results[-1], prior_best, round_score)

            # --- Priority 1: Per-round structured metrics.json ---
            self._write_round_metrics_json(outer, all_round_results[-1], round_score, round_elapsed)
            # Accumulate run-level tool call counts from the round's inner results
            for _pr in all_round_results[-1]:
                for _ir in _pr.inner_results:
                    _tcl = getattr(_ir, "tool_call_log", None) or []
                    _total_tool_calls += len(_tcl)
                    _total_tool_errors += sum(1 for t in _tcl if not t.get("success", True))

            # Early stopping — always update best_round_score, but only count
            # non-improvement from round 2 onwards.  Round 1 (outer == 0)
            # establishes the baseline; penalising it as a "no improvement"
            # event is wrong because there is nothing to compare against yet.
            # The old guard `round_score > 0` caused zero-scoring rounds to be
            # silently skipped, so patience never fired when all rounds scored
            # 0.0.  We now track unconditionally but skip the increment on the
            # first round.
            if self.config.patience > 0:
                if round_score > best_round_score:
                    best_round_score = round_score
                    no_improve_count = 0
                    log.info("  ↑ New best score: %.1f", best_round_score)
                elif outer == 0:
                    # First round: record the baseline, do not penalise.
                    best_round_score = round_score
                    log.info("  ↑ Baseline score: %.1f", best_round_score)
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

        self._metrics_collector.flush()

        detail_path = str(
            Path(self.config.harness.workspace) / "pipeline_round_details.jsonl"
        )
        self._metrics_collector.flush_detail(detail_path)

        # Final summary
        self.artifacts.write_final_summary(
            f"# Pipeline Complete\n\n"
            f"Rounds: {len(all_round_results)}\n"
            f"Best score: {best_round_score:.1f}\n"
            f"Elapsed: {total_elapsed:.1f}s\n\n"
            f"## Final Proposal\n\n{prior_best or '(none)'}\n"
        )

        # --- Priority 2: Write run-level summary.json ---
        self._write_run_summary(
            rounds_completed=len(all_round_results),
            best_score=best_round_score,
            score_history=score_history,
            total_elapsed=total_elapsed,
            total_tool_calls=_total_tool_calls,
            total_tool_errors=_total_tool_errors,
        )

        log.info(
            "Pipeline complete: rounds=%d  best=%.1f  total=%.1fs  artifacts=%s",
            len(all_round_results), best_round_score, total_elapsed, self.artifacts.run_dir,
        )

        # --- Priority 5: Auto-tag when best_score > 7.0 ---
        await self._maybe_auto_tag(best_round_score, len(all_round_results))

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
                log.info(
                    "  phase=%s  status=skipped (skip_after_round=%s, skip_cycle=%s, outer=%d)",
                    phase.label, phase.skip_after_round, phase.skip_cycle, outer,
                )
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
                self._metrics_collector.record_phase(phase.name, phase_result)
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

        round_score = (sum(phase_scores) / len(phase_scores)) if phase_scores else 0.0
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

    def _write_round_metrics_json(
        self,
        outer: int,
        results: list[PhaseResult],
        round_score: float,
        elapsed_s: float,
    ) -> None:
        """Write per-round structured metrics.json inside the round artifact dir.

        Schema (Priority 1):
        {
            "round": <int>,
            "score": <float>,
            "elapsed_s": <float>,
            "phases": [
                {
                    "phase": <label>,
                    "best_score": <float>,
                    "inner_rounds": <int>,
                    "tool_calls": <int>   // sum across inner rounds
                },
                ...
            ]
        }
        """
        phases_data = []
        for r in results:
            # Use structured tool_call_log (populated in _run_implement_round).
            # Fall back to line-count for any round where the field is absent
            # (debate-mode rounds, resumed rounds without the field).
            total_tool_calls = 0
            error_tool_calls = 0
            for ir in r.inner_results:
                tcl = getattr(ir, "tool_call_log", None) or []
                if tcl:
                    total_tool_calls += len(tcl)
                    error_tool_calls += sum(1 for t in tcl if not t.get("success", True))
                else:
                    # Fallback: count "- " prefixed lines in implement_log
                    impl_log = getattr(ir, "implement_log", "") or ""
                    if impl_log:
                        total_tool_calls += impl_log.count("\n- ")
            phases_data.append({
                "phase": r.phase.label,
                "best_score": round(r.best_score, 2),
                "inner_rounds": len(r.inner_results),
                "tool_calls": total_tool_calls,
                "tool_error_calls": error_tool_calls,
            })
        total_round_calls = sum(p["tool_calls"] for p in phases_data)
        total_round_errors = sum(p["tool_error_calls"] for p in phases_data)
        payload = {
            "round": outer + 1,
            "score": round(round_score, 2),
            "elapsed_s": round(elapsed_s, 2),
            "phases": phases_data,
            "tool_calls_total": total_round_calls,
            "tool_error_rate": round(total_round_errors / total_round_calls, 3) if total_round_calls else 0.0,
        }
        try:
            self.artifacts.write(
                json.dumps(payload, indent=2),
                f"round_{outer + 1}",
                "metrics.json",
            )
        except Exception as exc:
            log.warning("_write_round_metrics_json: failed to write metrics: %s", exc)

    def _write_run_summary(
        self,
        rounds_completed: int,
        best_score: float,
        score_history: list[dict],
        total_elapsed: float,
        total_tool_calls: int = 0,
        total_tool_errors: int = 0,
    ) -> None:
        """Write run-level summary.json at the root of the artifact store.

        Schema (Priority 2):
        {
            "total_rounds": <int>,
            "best_score": <float>,
            "score_history": [{"round": <int>, "score": <float>}, ...],
            "tool_error_rate": <float>,   // fraction of calls that returned an error
            "total_tool_calls": <int>,
            "elapsed_total_s": <float>
        }
        """
        tool_error_rate = (
            round(total_tool_errors / total_tool_calls, 3)
            if total_tool_calls > 0 else 0.0
        )
        payload: dict = {
            "total_rounds": rounds_completed,
            "best_score": round(best_score, 2),
            "score_history": score_history,
            "tool_error_rate": tool_error_rate,
            "total_tool_calls": total_tool_calls,
            "elapsed_total_s": round(total_elapsed, 2),
        }
        payload["metrics_tool_turns"] = self._metrics_collector.total_tool_turns
        try:
            self.artifacts.write(json.dumps(payload, indent=2), "summary.json")
            log.info(
                "summary.json written: rounds=%d best=%.1f tool_error_rate=%.3f elapsed=%.1fs",
                rounds_completed, best_score, tool_error_rate, total_elapsed,
            )
        except Exception as exc:
            log.warning("_write_run_summary: failed to write summary.json: %s", exc)

    async def _maybe_auto_tag(self, best_score: float, rounds_completed: int) -> None:
        """Create an annotated git tag when best_score > 7.0 (Priority 5).

        Tag format: ``v-auto-score{score:.1f}-r{rounds}``
        Example:    ``v-auto-score8.3-r5``

        Only tags when:
        - best_score > 7.0
        - the workspace is inside a git repository
        - the tag does not already exist (avoids duplicate-tag errors)

        Failures are logged as warnings and never propagate — tagging is a
        best-effort observability feature and must not abort the pipeline.
        """
        _AUTO_TAG_THRESHOLD = 7.0
        if best_score <= _AUTO_TAG_THRESHOLD:
            log.info(
                "auto_tag: skipped (best_score=%.1f ≤ %.1f threshold)",
                best_score, _AUTO_TAG_THRESHOLD,
            )
            return

        import asyncio as _asyncio

        tag_name = f"v-auto-score{best_score:.1f}-r{rounds_completed}"
        tag_msg = (
            f"Auto-tag: pipeline run completed with best_score={best_score:.1f} "
            f"in {rounds_completed} round(s)"
        )
        workspace = self.config.harness.workspace

        async def _run_git(args: list[str]) -> tuple[int, str]:
            try:
                proc = await _asyncio.create_subprocess_exec(
                    "git", *args,
                    cwd=workspace,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.PIPE,
                )
                stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=15)
                return proc.returncode, (stdout + stderr).decode(errors="replace").strip()
            except Exception as exc:
                return -1, str(exc)

        # Check if tag already exists to avoid duplicate-tag errors
        rc, out = await _run_git(["tag", "-l", tag_name])
        if rc == 0 and out.strip() == tag_name:
            log.info("auto_tag: tag %r already exists — skipping", tag_name)
            return

        # Create annotated tag
        rc, out = await _run_git(["tag", "-a", tag_name, "-m", tag_msg])
        if rc == 0:
            log.info(
                "auto_tag: created tag %r (best_score=%.1f rounds=%d)",
                tag_name, best_score, rounds_completed,
            )
        else:
            log.warning(
                "auto_tag: failed to create tag %r — rc=%d output=%r",
                tag_name, rc, out,
            )


