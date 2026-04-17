"""PipelineLoop — outer rounds orchestrator for the phase pipeline."""

from __future__ import annotations

import asyncio as _asyncio
import datetime
import json
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.core.artifacts import ArtifactStore
from harness.core.checkpoint import CheckpointManager
from harness.core.config import PipelineConfig
from harness.core.llm import LLM
from harness.pipeline.health import HealthMonitor
from harness.pipeline.memory import MemoryStore
from harness.pipeline.metrics import MetricsCollector
from harness.pipeline.phase import PhaseConfig, PhaseResult
from harness.prompts.meta_review import META_REVIEW_SYSTEM, META_REVIEW_USER
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
        
        # Health monitoring for production quality
        self.health_monitor = HealthMonitor(config)

        # Graceful shutdown
        self._shutdown_requested: bool = False
        # Meta-review context injected into subsequent rounds
        self._meta_review_context: str = ""
        # Run metrics tracking
        self.meta_review_count: int = 0
        self.auto_push_count: int = 0
        self.total_phases_run: int = 0
        self.shutdown_reason: str = "completed"  # "completed", "signal", "max_rounds", "error"
        # Traceability improvements
        self.start_time: float = time.time()
        self.score_history: list[float] = []  # Track scores for trend detection
        self.score_trend_warnings: list[dict] = []  # Track score trend warnings
        self.phase_score_history: list[dict] = []  # Track phase-level scores with metadata

    def _build_phases(self) -> list[PhaseConfig]:
        """Build PhaseConfig list from raw config dicts."""
        return [PhaseConfig.from_dict(p) for p in self.config.phases]

    # ---- graceful shutdown ----

    def _request_shutdown(self) -> None:
        """Signal callback: request graceful shutdown after current phase."""
        if not self._shutdown_requested:
            self._shutdown_requested = True
            log.warning(
                "Shutdown requested (signal received). "
                "Finishing current phase, then exiting cleanly…"
            )

    def _install_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM to trigger graceful shutdown."""
        try:
            loop = _asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._request_shutdown)
            log.debug("Signal handlers installed (SIGINT, SIGTERM)")
        except NotImplementedError:
            # Windows: add_signal_handler is not supported.
            log.debug("Signal handlers not available on this platform")

    def _uninstall_signal_handlers(self) -> None:
        """Restore default signal handlers."""
        try:
            loop = _asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError):
            pass

    def _write_shutdown_state(
        self,
        outer: int,
        best_round_score: float,
        score_history: list[dict],
    ) -> None:
        """Persist shutdown state so the operator knows where the run stopped."""
        payload = {
            "reason": "signal",
            "completed_rounds": outer + 1,
            "best_score": round(best_round_score, 2),
            "score_history": score_history,
        }
        try:
            self.artifacts.write(json.dumps(payload, indent=2), "shutdown_state.json")
            log.info("Shutdown state written to shutdown_state.json")
        except Exception as exc:
            log.warning("Failed to write shutdown_state.json: %s", exc)

    # ---- meta-review ----

    async def _run_meta_review(
        self,
        outer: int,
        score_history: list[dict],
        all_round_results: list[list[PhaseResult]],
    ) -> str:
        """Run a meta-review across recent rounds and return the review text."""
        # Resume check
        if self.checkpoint.is_meta_review_done(outer):
            cached = self.artifacts.read(f"round_{outer + 1}", "meta_review.md") or ""
            log.info("Meta-review for round %d already done (resumed)", outer + 1)
            return cached

        interval = self.config.meta_review_interval
        start_round = max(0, len(all_round_results) - interval)

        # Collect evaluator critiques from recent rounds
        critiques_parts: list[str] = []
        for ri in range(start_round, len(all_round_results)):
            for pr in all_round_results[ri]:
                for ir in pr.inner_results:
                    ds = getattr(ir, "dual_score", None)
                    if ds is None:
                        continue
                    basic = getattr(ds, "basic", None)
                    diffusion = getattr(ds, "diffusion", None)
                    crit = ""
                    if basic and hasattr(basic, "critique"):
                        crit += f"Basic: {basic.critique}\n"
                    if diffusion and hasattr(diffusion, "critique"):
                        crit += f"Diffusion: {diffusion.critique}\n"
                    if crit:
                        critiques_parts.append(
                            f"Round {ri + 1} / {pr.phase.label} / inner {ir.inner_index + 1}:\n{crit}"
                        )

        # Score trend
        recent_scores = score_history[start_round:]
        score_trend = "\n".join(
            f"Round {s['round']}: {s['score']}" for s in recent_scores
        ) or "(no scores yet)"

        # Git delta (hash-based incremental review)
        git_delta = await self._get_git_delta()

        # Memory context
        memory_ctx = self.memory.format_context(None, max_entries=20)

        # Render prompt
        from string import Template
        user_prompt = Template(META_REVIEW_USER).safe_substitute(
            start_round=start_round + 1,
            end_round=outer + 1,
            score_trend=score_trend,
            git_delta=git_delta or "(no git changes or not a git repo)",
            critiques="\n\n".join(critiques_parts[-20:]) or "(no critiques collected)",
            memory_context=memory_ctx or "(no memory entries)",
        )

        system = self.config.meta_review_system or META_REVIEW_SYSTEM

        log.info("Running meta-review for rounds %d–%d…", start_round + 1, outer + 1)
        response = await self.llm.call(
            messages=[{"role": "user", "content": user_prompt}],
            system=system,
        )

        # Write artifact and checkpoint
        self.artifacts.write(response, f"round_{outer + 1}", "meta_review.md")
        self.checkpoint.mark_meta_review_done(outer)

        # Update review hash
        current_hash = await self._get_head_hash()
        if current_hash:
            self.checkpoint.write_last_review_hash(current_hash)

        log.info("Meta-review complete (%d chars)", len(response))
        return response

    async def _get_git_delta(self) -> str:
        """Get git changes since last meta-review hash."""
        last_hash = self.checkpoint.read_last_review_hash()
        if not last_hash:
            return ""
        rc, log_out = await self._run_git(["log", f"{last_hash}..HEAD", "--oneline", "-20"])
        if rc != 0:
            return ""
        rc, stat_out = await self._run_git(["diff", f"{last_hash}..HEAD", "--stat"])
        parts = []
        if log_out.strip():
            parts.append(f"Commits:\n{log_out.strip()}")
        if stat_out.strip():
            parts.append(f"Files changed:\n{stat_out.strip()}")
        return "\n\n".join(parts)

    async def _get_head_hash(self) -> str:
        """Get current HEAD commit hash, or '' if not a git repo."""
        rc, out = await self._run_git(["rev-parse", "HEAD"])
        return out.strip() if rc == 0 else ""

    async def _run_git(self, args: list[str]) -> tuple[int, str]:
        """Run a git command in the workspace. Returns (returncode, output)."""
        workspace = self.config.harness.workspace
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

    # ---- auto-push ----

    async def _maybe_auto_push(self, outer: int) -> None:
        """Push to remote every N rounds if configured."""
        if self.config.auto_push_interval <= 0:
            return
        if (outer + 1) % self.config.auto_push_interval != 0:
            return

        branch = self.config.auto_push_branch
        if not branch:
            rc, out = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            if rc != 0:
                log.warning("auto_push: could not determine current branch: %s", out)
                return
            branch = out.strip()

        remote = self.config.auto_push_remote

        rc_pull, out_pull = await self._run_git(
            ["pull", "--rebase", remote, branch]
        )
        if rc_pull != 0:
            if "CONFLICT" in out_pull or "could not apply" in out_pull:
                await self._run_git(["rebase", "--abort"])
                log.warning(
                    "auto_push: pull --rebase conflicted — aborted, skipping push. "
                    "output=%r", out_pull[:300],
                )
                return
            log.warning("auto_push: pull --rebase failed (rc=%d) — trying push anyway. output=%r",
                        rc_pull, out_pull[:300])

        rc, out = await self._run_git(["push", remote, branch])
        if rc == 0:
            log.info("auto_push: pushed to %s/%s", remote, branch)
            self.auto_push_count += 1
        else:
            log.warning("auto_push: push failed — rc=%d output=%r", rc, out)

    # ---- manual mode pause ----

    async def _manual_review_pause(self, meta_review_text: str) -> str:
        """Pause for human input after meta-review. Returns human feedback."""
        import sys

        print("\n" + "=" * 60)
        print("META-REVIEW COMPLETE — Manual mode pause")
        print("=" * 60)
        print(meta_review_text[:3000])
        if len(meta_review_text) > 3000:
            print(f"\n… [{len(meta_review_text) - 3000} chars truncated]")
        print("=" * 60)
        print("Enter feedback (empty line to continue, 'quit' to stop):")

        loop = _asyncio.get_running_loop()
        lines: list[str] = []
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            line = line.rstrip("\n")
            if line == "":
                break
            if line.lower() == "quit":
                self._shutdown_requested = True
                break
            lines.append(line)

        feedback = "\n".join(lines)
        if feedback:
            self.artifacts.write(
                feedback,
                f"round_{self._current_outer + 1}",
                "human_feedback.txt",
            )
        return feedback

    # ---- prompt auto-update ----

    async def _auto_update_prompts(
        self,
        meta_review_text: str,
        phases: list[PhaseConfig],
        outer: int,
    ) -> list[PhaseConfig]:
        """Use LLM to rewrite phase prompts based on meta-review suggestions."""
        from string import Template
        import copy

        updated: list[PhaseConfig] = []
        version = (outer + 1) // max(self.config.meta_review_interval, 1)

        for phase in phases:
            if not phase.system_prompt:
                updated.append(phase)
                continue

            prompt = (
                f"You are rewriting a pipeline phase prompt based on meta-review feedback.\n\n"
                f"## Current Prompt\n```\n{phase.system_prompt[:4000]}\n```\n\n"
                f"## Meta-Review Suggestions\n{meta_review_text[:3000]}\n\n"
                f"Rewrite the prompt to address the suggestions. "
                f"CRITICAL: preserve ALL template variables ($file_context, $prior_best, "
                f"$syntax_errors, $falsifiable_criterion, etc.) exactly as they appear. "
                f"Output ONLY the new prompt text, nothing else."
            )
            try:
                new_prompt = await self.llm.call(
                    messages=[{"role": "user", "content": prompt}],
                    system="You rewrite prompts. Output only the new prompt.",
                )
            except Exception as exc:
                log.warning(
                    "auto_update_prompts: failed for phase %s: %s", phase.label, exc
                )
                updated.append(phase)
                continue

            # Validate: all $variables from original must be in new
            import re as _re
            orig_vars = set(_re.findall(r"\$\w+", phase.system_prompt))
            new_vars = set(_re.findall(r"\$\w+", new_prompt))
            missing = orig_vars - new_vars
            if missing:
                log.warning(
                    "auto_update_prompts: phase %s — new prompt missing variables %s, "
                    "keeping original",
                    phase.label, missing,
                )
                updated.append(phase)
                continue

            # Write version history
            self.artifacts.write(
                new_prompt,
                f"round_{outer + 1}",
                "prompt_versions",
                f"{phase.label}_v{version}.txt",
            )

            new_phase = copy.copy(phase)
            object.__setattr__(new_phase, "system_prompt", new_prompt)
            updated.append(new_phase)
            log.info(
                "auto_update_prompts: updated phase %s prompt (v%d, %d→%d chars)",
                phase.label, version, len(phase.system_prompt), len(new_prompt),
            )

        # Write history log (append to JSONL)
        history_entry = json.dumps({
            "round": outer + 1,
            "version": version,
            "phases_updated": [
                p.label for p, orig in zip(updated, phases)
                if p.system_prompt != orig.system_prompt
            ],
        })
        existing = self.artifacts.read("prompt_history.jsonl") or ""
        self.artifacts.write(existing + history_entry + "\n", "prompt_history.jsonl")
        return updated

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

        self._shutdown_requested = False
        self._install_signal_handlers()

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
                        # Get all scores in the decline streak (including the starting score)
                        # For a streak of length N, we need the last N+1 scores
                        streak_scores = []
                        for i in range(_decline_streak + 1):
                            # score_history[-1] is current round, score_history[-2] is previous, etc.
                            idx = -1 - i
                            streak_scores.append(score_history[idx]["score"])
                        # Reverse to show in chronological order
                        streak_scores.reverse()
                        
                        warning_msg = (
                            f"TREND WARNING: score has declined for {_decline_streak} consecutive "
                            f"round(s) ({' → '.join(f'{s:.2f}' for s in streak_scores)}). "
                            f"Consider adjusting the prompt or stopping early."
                        )
                        log.warning(warning_msg)
                        # Store warning for inclusion in summary
                        self.score_trend_warnings.append({
                            "round": outer + 1,
                            "decline_streak": _decline_streak,
                            "message": warning_msg,
                            "scores": streak_scores
                        })
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

            # --- Meta-review: periodic cross-round analysis ---
            self._current_outer = outer  # expose to _manual_review_pause
            if (
                self.config.meta_review_interval > 0
                and (outer + 1) % self.config.meta_review_interval == 0
                and outer + 1 < self.config.outer_rounds  # skip on last round
            ):
                meta_text = await self._run_meta_review(
                    outer, score_history, all_round_results,
                )
                if meta_text:
                    self.meta_review_count += 1
                if self.config.run_mode == "manual" and meta_text:
                    feedback = await self._manual_review_pause(meta_text)
                    if feedback:
                        self._meta_review_context = (
                            meta_text[:2000]
                            + "\n\n## Human Feedback\n\n" + feedback[:1000]
                        )
                    elif self.config.meta_review_inject:
                        self._meta_review_context = meta_text[:3000]
                elif self.config.meta_review_inject and meta_text:
                    self._meta_review_context = meta_text[:3000]

                # Auto-update prompts based on meta-review
                if self.config.auto_update_prompts and meta_text:
                    phases = await self._auto_update_prompts(meta_text, phases, outer)

            # --- Auto-push ---
            await self._maybe_auto_push(outer)

            # --- Auto-tag (per-round) ---
            if self.config.auto_tag_interval > 0 and (outer + 1) % self.config.auto_tag_interval == 0:
                # Use max(running best, this round) so patience=0 still tags correctly
                tag_score = max(best_round_score, round_score)
                await self._maybe_auto_tag(tag_score, outer + 1, source="per_round")

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

            # Graceful shutdown check (between rounds)
            if self._shutdown_requested:
                log.info(
                    "Graceful shutdown after round %d/%d",
                    outer + 1, self.config.outer_rounds,
                )
                self.shutdown_reason = "signal"
                self._write_shutdown_state(outer, best_round_score, score_history)
                break

        self._uninstall_signal_handlers()

        # Set shutdown reason if not already set by signal handler
        if self.shutdown_reason == "completed" and len(all_round_results) >= self.config.outer_rounds:
            self.shutdown_reason = "max_rounds"

        total_elapsed = time.monotonic() - pipeline_start

        self._metrics_collector.flush()

        detail_path = str(
            Path(self.config.harness.workspace) / "pipeline_round_details.jsonl"
        )
        self._metrics_collector.flush_detail(detail_path)

        # Final summary
        stop_reason = "shutdown" if self._shutdown_requested else "complete"
        self.artifacts.write_final_summary(
            f"# Pipeline {stop_reason.title()}\n\n"
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
            "Pipeline %s: rounds=%d  best=%.1f  total=%.1fs  artifacts=%s",
            stop_reason, len(all_round_results), best_round_score,
            total_elapsed, self.artifacts.run_dir,
        )

        # --- Priority 5: Auto-tag when best_score > 7.0 ---
        await self._maybe_auto_tag(best_round_score, len(all_round_results))

        return PipelineResult(
            success=not self._shutdown_requested,
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
            # Graceful shutdown: skip remaining phases
            if self._shutdown_requested:
                log.info("  phase=%s  status=skipped (shutdown requested)", phase.label)
                break

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
                    # Track resumed phase score history for traceability
                    self.phase_score_history.append({
                        "outer_round": outer + 1,
                        "phase": phase.label,
                        "phase_name": phase.name,
                        "score": round(resumed_score, 2),
                        "inner_results": 0,  # Unknown for resumed phases
                        "elapsed_s": 0.0,  # Not measured for resumed phases
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "status": "resumed",
                    })
                # Count resumed phases for total phases run tracking
                self.total_phases_run += 1
                continue

            phase_start = time.monotonic()
            try:
                # Inject memory + meta-review context into the carry-forward
                # text so the LLM can build on accumulated learnings.
                memory_ctx = self.memory.format_context(phase.label, max_entries=8)
                phase_prior = prior_best
                if self._meta_review_context:
                    phase_prior = (
                        "## Meta-Review Findings\n\n"
                        + self._meta_review_context
                        + "\n\n"
                        + (phase_prior or "")
                    )
                if memory_ctx:
                    phase_prior = (
                        memory_ctx
                        + ("\n\n" + phase_prior if phase_prior else "")
                    )

                phase_result = await self.runner.run_phase(outer, phase, phase_prior)
                results.append(phase_result)
                phase_scores.append(phase_result.best_score)
                prior_best = phase_result.synthesis or prior_best
                # Record learnings for future rounds
                self.memory.record(outer, phase_result)
                self._metrics_collector.record_phase(phase.name, phase_result)
                # Track phase score history for traceability
                self.phase_score_history.append({
                    "outer_round": outer + 1,
                    "phase": phase.label,
                    "phase_name": phase.name,
                    "score": round(phase_result.best_score, 2),
                    "inner_results": len(phase_result.inner_results),
                    "elapsed_s": round(time.monotonic() - phase_start, 2),
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                # Increment total phases run counter for traceability
                self.total_phases_run += 1
                _phase_elapsed = time.monotonic() - phase_start
                log.info(
                    "  phase=%s  status=done  score=%.1f  elapsed=%.1fs  memory_entries=%d  total_phases=%d",
                    phase.label, phase_result.best_score, _phase_elapsed,
                    self.memory.entry_count, self.total_phases_run,
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
                        "total_phases_run": self.total_phases_run,
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

    def _load_persisted_score_history(self) -> list[dict]:
        """Load score history from persisted metrics.json files.
        
        This makes score trend detection checkpoint-resilient by reading
        the complete history from disk, not just the in-memory list.
        """
        score_history = []
        round_num = 1
        
        while True:
            metrics_path = self.artifacts.path(f"round_{round_num}", "metrics.json")
            if not metrics_path.exists():
                break
            try:
                content = metrics_path.read_text(encoding="utf-8")
                data = json.loads(content)
                score_history.append({
                    "round": round_num,
                    "score": data.get("score", 0.0)
                })
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                log.warning("Failed to load metrics.json for round %d: %s", round_num, exc)
            round_num += 1
        
        return score_history

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
            "elapsed_total_s": <float>,
            "end_time": <iso8601>,
            "total_phases_run": <int>,
            "shutdown_reason": <str>,
            "meta_review_count": <int>,
            "auto_push_count": <int>
        }
        """
        import datetime
        
        tool_error_rate = (
            round(total_tool_errors / total_tool_calls, 3)
            if total_tool_calls > 0 else 0.0
        )
        payload: dict = {
            "total_rounds": rounds_completed,
            "best_score": round(best_score, 2),
            "score_history": score_history,
            "phase_score_history": self.phase_score_history,
            "score_trend_warnings": self.score_trend_warnings,
            "tool_error_rate": tool_error_rate,
            "total_tool_calls": total_tool_calls,
            "elapsed_total_s": round(total_elapsed, 2),
            "start_time": datetime.datetime.fromtimestamp(self.start_time, datetime.timezone.utc).isoformat(),
            "end_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_phases_run": self.total_phases_run,
            "shutdown_reason": self.shutdown_reason,
            "meta_review_count": self.meta_review_count,
            "auto_push_count": self.auto_push_count,
        }
        payload["metrics_tool_turns"] = self._metrics_collector.total_tool_turns
        try:
            self.artifacts.write(json.dumps(payload, indent=2), "summary.json")
            log.info(
                "summary.json written: rounds=%d best=%.1f tool_error_rate=%.3f elapsed=%.1fs phases=%d",
                rounds_completed, best_score, tool_error_rate, total_elapsed,
                payload["total_phases_run"],
            )
        except Exception as exc:
            log.warning("_write_run_summary: failed to write summary.json: %s", exc)

    async def _maybe_auto_tag(
        self,
        best_score: float,
        rounds_completed: int,
        *,
        source: str = "end_of_run",
    ) -> None:
        """Create (and optionally push) an annotated git tag.

        ``source='end_of_run'`` is the legacy call from ``run()``'s tail; it
        becomes a no-op when ``auto_tag_interval > 0`` (per-round tagging
        takes over).  ``source='per_round'`` is the new path called from the
        outer loop alongside auto-push.

        Tag name: ``{auto_tag_prefix}-{rounds}-{shortsha}`` (legacy prefix
        ``v-auto`` retained as default).  Including the short SHA avoids
        collisions when a fresh process restart re-tags at the same round
        number on a different commit.

        Only tags when ``best_score >= auto_tag_min_score``.  Pushes the tag
        when ``auto_tag_push`` is true (uses ``auto_push_remote``).

        Failures are logged as warnings — tagging is best-effort and must
        never abort the pipeline.
        """
        # End-of-run path with auto_tag_at_end takes precedence: it bypasses
        # both the interval-suppression and the min_score gate.  This is what
        # guarantees a tag for self-improvement loops where the next chunk is
        # only triggered by a tag push.
        force_at_end = (source == "end_of_run") and self.config.auto_tag_at_end

        if source == "end_of_run" and self.config.auto_tag_interval > 0 and not force_at_end:
            return

        threshold = self.config.auto_tag_min_score
        if best_score < threshold and not force_at_end:
            log.info(
                "auto_tag: skipped (best_score=%.1f < %.1f threshold)",
                best_score, threshold,
            )
            return

        # When forcing at end-of-run, the branch may have unpushed commits
        # (patience early stop, or interval not reached).  Push the branch
        # first so the tag refers to commits the remote already has — without
        # this the tag push fails with 'missing necessary objects'.
        if force_at_end and self.config.auto_tag_push:
            branch = self.config.auto_push_branch
            if not branch:
                rc_b, out_b = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
                branch = out_b.strip() if rc_b == 0 else ""
            if branch and branch != "HEAD":
                remote = self.config.auto_push_remote
                rc_pull, out_pull = await self._run_git(
                    ["pull", "--rebase", remote, branch]
                )
                if rc_pull != 0:
                    if "CONFLICT" in out_pull or "could not apply" in out_pull:
                        await self._run_git(["rebase", "--abort"])
                        log.warning(
                            "auto_tag(end): pull --rebase conflicted — aborted. output=%r",
                            out_pull[:300],
                        )
                    else:
                        log.warning("auto_tag(end): pull --rebase failed (rc=%d)", rc_pull)

                rc, out = await self._run_git(["push", remote, branch])
                if rc == 0:
                    log.info("auto_tag(end): pushed branch %s before tagging", branch)
                else:
                    log.warning(
                        "auto_tag(end): pre-tag branch push failed — rc=%d output=%r",
                        rc, out,
                    )

        rc_sha, sha_out = await self._run_git(["rev-parse", "--short=7", "HEAD"])
        short_sha = sha_out.strip() if rc_sha == 0 and sha_out.strip() else "nosha"
        tag_name = f"{self.config.auto_tag_prefix}-{rounds_completed}-{short_sha}"
        tag_msg = (
            f"Auto-tag: best_score={best_score:.1f} rounds={rounds_completed} "
            f"sha={short_sha}"
        )

        rc, out = await self._run_git(["tag", "-l", tag_name])
        if rc == 0 and out.strip() == tag_name:
            log.info("auto_tag: tag %r already exists — skipping", tag_name)
            return

        rc, out = await self._run_git(["tag", "-a", tag_name, "-m", tag_msg])
        if rc != 0:
            log.warning(
                "auto_tag: failed to create tag %r — rc=%d output=%r",
                tag_name, rc, out,
            )
            return
        log.info(
            "auto_tag: created tag %r (best_score=%.1f rounds=%d)",
            tag_name, best_score, rounds_completed,
        )

        if self.config.auto_tag_push:
            remote = self.config.auto_push_remote
            rc, out = await self._run_git(["push", remote, tag_name])
            if rc == 0:
                log.info("auto_tag: pushed tag %r to %s", tag_name, remote)
            else:
                log.warning(
                    "auto_tag: push failed for tag %r — rc=%d output=%r",
                    tag_name, rc, out,
                )


