"""PhaseRunner — executes a single phase: inner rounds → synthesis → hooks."""

from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import Any

from harness.artifacts import ArtifactStore
from harness.checkpoint import CheckpointManager
from harness.config import HarnessConfig, PipelineConfig
from harness.dual_evaluator import DualEvaluator
from harness.hooks import HookResult, VerificationHook, build_hooks
from harness.llm import LLM
from harness.phase import DualScore, InnerResult, PhaseConfig, PhaseResult, ScoreItem
from harness.prompts import synthesis as synth_prompts
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MIN_SYNTHESIS_CHARS = 150


def _read_source_files(workspace: str, glob_patterns: list[str]) -> str:
    """Read and concatenate source files matching glob patterns."""
    import glob as glob_mod

    parts: list[str] = []
    for pattern in glob_patterns:
        for path_str in sorted(glob_mod.glob(pattern, recursive=True, root_dir=workspace)):
            full = Path(workspace) / path_str
            if full.is_file():
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"=== FILE: {path_str} ===\n{content}\n")
                except OSError:
                    continue
    return "".join(parts) or "[No source files matched]\n"


class PhaseRunner:
    """Executes a single phase: inner rounds + synthesis + verification hooks."""

    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        pipeline_config: PipelineConfig,
        artifacts: ArtifactStore,
        checkpoint: CheckpointManager,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.config = pipeline_config
        self.harness = pipeline_config.harness
        self.artifacts = artifacts
        self.checkpoint = checkpoint
        self.dual_evaluator = DualEvaluator(llm)

    async def run_phase(
        self,
        outer: int,
        phase: PhaseConfig,
        prior_best: str | None,
    ) -> PhaseResult:
        """Run all inner rounds, synthesize, run hooks, return result."""
        label = phase.label
        n_inner = phase.inner_rounds or self.config.inner_rounds

        # 1. Read source files once for the whole phase
        file_context = _read_source_files(self.harness.workspace, phase.glob_patterns)
        log.info(
            "Phase %s: injected %d chars from source files", label, len(file_context)
        )

        # 2. Run inner rounds
        all_results: list[InnerResult] = []
        best_result: InnerResult | None = None
        carry_syntax_errors = ""

        for inner in range(n_inner):
            # Resume check
            if self.checkpoint.is_inner_done(outer, label, inner):
                result = self._load_inner_result(outer, label, inner)
                log.info("Phase %s inner %d: resumed from disk", label, inner + 1)
            else:
                current_prior = self._select_prior(
                    inner, prior_best, best_result
                )
                result = await self._run_inner_round(
                    outer, phase, inner, file_context,
                    current_prior, carry_syntax_errors,
                )
                self.checkpoint.mark_inner_done(outer, label, inner)

            all_results.append(result)
            carry_syntax_errors = result.syntax_errors

            if best_result is None or result.combined_score > best_result.combined_score:
                best_result = result

        # 3. Run verification hooks (implement mode only)
        if phase.mode == "implement":
            hooks = build_hooks(phase)
            for hook in hooks:
                ctx = {"outer": outer, "phase_name": phase.name}
                hook_result = await hook.run(self.harness, ctx)
                log.info("Hook %s: passed=%s", hook.name, hook_result.passed)

        # 4. Synthesis
        synthesis = await self._run_synthesis(
            outer, phase, all_results, file_context
        )

        best_score = max(r.combined_score for r in all_results) if all_results else 0.0

        # 5. Write phase summary
        self._write_phase_summary(outer, phase, all_results, synthesis, best_score)
        self.checkpoint.mark_phase_done(outer, label)

        return PhaseResult(
            phase=phase,
            synthesis=synthesis,
            best_score=best_score,
            inner_results=all_results,
        )

    async def _run_inner_round(
        self,
        outer: int,
        phase: PhaseConfig,
        inner: int,
        file_context: str,
        prior_best: str | None,
        syntax_errors: str,
    ) -> InnerResult:
        """Execute one inner round — debate or implement based on phase.mode."""
        label = phase.label
        segs = self.artifacts.inner_dir(outer, label, inner)

        # Build executor prompt
        prompt = self._build_executor_prompt(phase, file_context, prior_best, syntax_errors)
        self.artifacts.write(prompt, *segs, "executor_prompt.txt")

        if phase.mode == "implement":
            return await self._run_implement_round(
                outer, phase, inner, prompt, file_context, segs
            )
        return await self._run_debate_round(
            outer, phase, inner, prompt, file_context, segs
        )

    async def _run_debate_round(
        self,
        outer: int,
        phase: PhaseConfig,
        inner: int,
        prompt: str,
        file_context: str,
        segs: tuple[str, ...],
    ) -> InnerResult:
        """Debate mode: text-only proposal, no tool_use."""
        log.info("Phase %s inner %d: debating...", phase.label, inner + 1)

        resp = await self.llm.call(
            [{"role": "user", "content": prompt}],
            system="You are a senior software engineer. Produce a detailed, actionable proposal.",
        )
        proposal = resp.text
        self.artifacts.write(proposal, *segs, "proposal.txt")

        # Dual evaluation
        dual_score = await self.dual_evaluator.evaluate(
            proposal, file_context,
            basic_system=self.config.dual_evaluator.basic_system,
            diffusion_system=self.config.dual_evaluator.diffusion_system,
        )
        self.artifacts.write(dual_score.basic.critique, *segs, "basic_eval.txt")
        self.artifacts.write(dual_score.diffusion.critique, *segs, "diffusion_eval.txt")

        log.info(
            "Phase %s inner %d: basic=%.1f diffusion=%.1f combined=%.1f",
            phase.label, inner + 1,
            dual_score.basic.score, dual_score.diffusion.score, dual_score.combined,
        )

        return InnerResult(proposal=proposal, dual_score=dual_score)

    async def _run_implement_round(
        self,
        outer: int,
        phase: PhaseConfig,
        inner: int,
        prompt: str,
        file_context: str,
        segs: tuple[str, ...],
    ) -> InnerResult:
        """Implement mode: tool_use edits real files, then evaluate code state."""
        log.info("Phase %s inner %d: implementing...", phase.label, inner + 1)

        text, exec_log = await self.llm.call_with_tools(
            [{"role": "user", "content": prompt}],
            self.registry,
            system="You are a precise code executor. Follow the plan step by step using the tools available.",
        )
        implement_log = "\n".join(
            f"- {e['tool']}: {e['output'][:200]}" for e in exec_log
        )
        self.artifacts.write(implement_log, *segs, "implement_output.txt")

        # Re-read code state
        code_state = _read_source_files(self.harness.workspace, phase.glob_patterns)
        self.artifacts.write(code_state, *segs, "post_impl_snapshot.txt")

        # Dual evaluation on code state
        eval_subject = f"## Implementation Log\n\n{implement_log}\n\n## Code State After\n\n{code_state[:8000]}"
        dual_score = await self.dual_evaluator.evaluate(
            eval_subject, file_context,
            basic_system=self.config.dual_evaluator.basic_system,
            diffusion_system=self.config.dual_evaluator.diffusion_system,
        )
        self.artifacts.write(dual_score.basic.critique, *segs, "basic_eval.txt")
        self.artifacts.write(dual_score.diffusion.critique, *segs, "diffusion_eval.txt")

        # Syntax check (inline, not hook — needed for carry-forward)
        syntax_errors = ""
        if phase.syntax_check_patterns:
            from harness.hooks import SyntaxCheckHook
            hook = SyntaxCheckHook(phase.syntax_check_patterns)
            hook_result = await hook.run(self.harness, {})
            syntax_errors = hook_result.errors
            self.artifacts.write(syntax_errors, *segs, "syntax_errors.txt")

        log.info(
            "Phase %s inner %d: basic=%.1f diffusion=%.1f syntax=%s",
            phase.label, inner + 1,
            dual_score.basic.score, dual_score.diffusion.score,
            "OK" if not syntax_errors else "ERRORS",
        )

        return InnerResult(
            proposal=text,
            dual_score=dual_score,
            implement_log=implement_log,
            post_impl_snapshot=code_state,
            syntax_errors=syntax_errors,
        )

    async def _run_synthesis(
        self,
        outer: int,
        phase: PhaseConfig,
        results: list[InnerResult],
        file_context: str,
    ) -> str:
        """Merge inner round results into a single recommendation."""
        label = phase.label

        # Resume check
        if self.checkpoint.is_synthesis_done(outer, label):
            segs = self.artifacts.phase_dir(outer, label)
            return self.artifacts.read(*segs, "synthesis.txt")

        # Build round data
        round_parts: list[str] = []
        for i, r in enumerate(results):
            score_info = f"combined={r.combined_score:.1f}"
            if r.dual_score:
                score_info = f"basic={r.dual_score.basic.score:.1f}, diffusion={r.dual_score.diffusion.score:.1f}"
            round_parts.append(
                f"=== Round {i + 1} ({score_info}) ===\n"
                f"{r.proposal[:3000]}\n"
            )
            if r.dual_score:
                round_parts.append(
                    f"--- Basic Evaluator ---\n{r.dual_score.basic.critique[:1500]}\n"
                    f"--- Diffusion Evaluator ---\n{r.dual_score.diffusion.critique[:1500]}\n"
                )

        round_data = "\n".join(round_parts)
        user_msg = Template(synth_prompts.SYNTHESIS_USER_TEMPLATE).safe_substitute(
            phase_name=phase.name,
            file_context=file_context[:5000],
            round_data=round_data,
            falsifiable_criterion=phase.falsifiable_criterion,
        )
        synth_sys = self.config.synthesis_system or synth_prompts.SYNTHESIS_SYSTEM

        # First attempt
        resp = await self.llm.call(
            [{"role": "user", "content": user_msg}], system=synth_sys
        )
        synthesized = resp.text

        # Retry if too short
        if len(synthesized.strip()) < self.config.min_synthesis_chars:
            log.warning("Synthesis too short (%d chars), retrying...", len(synthesized))
            retry_msg = "[PRIOR ATTEMPT FAILED — output was too short. Please produce a complete synthesis.]\n\n" + user_msg
            resp = await self.llm.call(
                [{"role": "user", "content": retry_msg}], system=synth_sys
            )
            synthesized = resp.text

        # Fallback to best inner round
        if len(synthesized.strip()) < self.config.min_synthesis_chars and results:
            best = max(results, key=lambda r: r.combined_score)
            synthesized = f"[SYNTHESIS FALLBACK — best inner round]\n\n{best.proposal}"

        # Persist
        segs = self.artifacts.phase_dir(outer, label)
        self.artifacts.write(synthesized, *segs, "synthesis.txt")
        self.checkpoint.mark_synthesis_done(outer, label)
        log.info("Phase %s synthesis: %d chars", label, len(synthesized))

        return synthesized

    # ---- helpers ----

    def _build_executor_prompt(
        self,
        phase: PhaseConfig,
        file_context: str,
        prior_best: str | None,
        syntax_errors: str,
    ) -> str:
        """Build the executor prompt from the phase template."""
        prior_section = ""
        if prior_best:
            prior_section = (
                "## Prior Best (improve upon this, do not repeat verbatim)\n\n"
                f"{prior_best}\n\n"
            )
        syntax_section = ""
        if syntax_errors:
            syntax_section = (
                "## PRIORITY FIX — Syntax errors must be fixed before new features\n\n"
                f"```\n{syntax_errors}\n```\n\n"
            )

        return Template(phase.system_prompt).safe_substitute(
            file_context=file_context,
            prior_best=prior_section,
            syntax_errors=syntax_section,
            falsifiable_criterion=phase.falsifiable_criterion,
        )

    def _select_prior(
        self,
        inner: int,
        initial_prior: str | None,
        best_result: InnerResult | None,
    ) -> str | None:
        if inner == 0:
            return initial_prior
        return best_result.proposal if best_result else initial_prior

    def _load_inner_result(
        self, outer: int, label: str, inner: int
    ) -> InnerResult:
        """Reconstruct an InnerResult from disk artifacts."""
        from harness.dual_evaluator import parse_score

        segs = self.artifacts.inner_dir(outer, label, inner)
        proposal = self.artifacts.read(*segs, "proposal.txt")
        basic_raw = self.artifacts.read(*segs, "basic_eval.txt")
        diff_raw = self.artifacts.read(*segs, "diffusion_eval.txt")

        return InnerResult(
            proposal=proposal or self.artifacts.read(*segs, "implement_output.txt"),
            dual_score=DualScore(
                basic=ScoreItem(parse_score(basic_raw), basic_raw),
                diffusion=ScoreItem(parse_score(diff_raw), diff_raw),
            ),
            implement_log=self.artifacts.read(*segs, "implement_output.txt"),
            post_impl_snapshot=self.artifacts.read(*segs, "post_impl_snapshot.txt"),
            syntax_errors=self.artifacts.read(*segs, "syntax_errors.txt"),
            pytest_result=self.artifacts.read(*segs, "pytest_result.txt"),
        )

    def _write_phase_summary(
        self,
        outer: int,
        phase: PhaseConfig,
        results: list[InnerResult],
        synthesis: str,
        best_score: float,
    ) -> None:
        """Write a markdown summary for the phase."""
        segs = self.artifacts.phase_dir(outer, phase.label)
        round_lines = "\n".join(
            f"- Inner {i + 1}: score={r.combined_score:.1f}"
            for i, r in enumerate(results)
        )
        content = (
            f"# Phase {phase.label} — Round {outer + 1}\n\n"
            f"## Scores\n\n{round_lines}\n\n"
            f"**Best**: {best_score:.1f}\n\n"
            f"## Synthesis\n\n{synthesis}\n"
        )
        self.artifacts.write(content, *segs, "phase_summary.txt")
