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
from harness.executor import EXECUTOR_SYSTEM
from harness.hooks import HookResult, VerificationHook, build_hooks
from harness.llm import LLM
from harness.phase import DualScore, InnerResult, PhaseConfig, PhaseResult, ScoreItem
from harness.prompts import synthesis as synth_prompts
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MIN_SYNTHESIS_CHARS = 150

# Default system prompt used in debate-mode rounds when a phase has no plain
# (non-template) system_prompt.  A concrete, opinionated default gets better
# proposals than the previous "You are a senior software engineer" stub.
_DEBATE_SYSTEM_DEFAULT = """\
You are a senior software engineer tasked with producing a detailed, \
actionable implementation proposal.

REQUIREMENTS FOR YOUR PROPOSAL:
1. Reference SPECIFIC code entities from the source context — actual function \
   names, class names, file paths. Generic descriptions ("the helper function") \
   score poorly.
2. Cover EVERY stated requirement. If a requirement is ambiguous, state your \
   interpretation explicitly.
3. For each change, state: FILE, WHAT changes, and WHY it is the right approach.
4. Anticipate at least one failure mode or edge case and explain how your \
   proposal handles it.
5. Keep the proposal concrete enough that another engineer could implement it \
   without asking clarifying questions.

DO NOT repeat the prior best verbatim — your goal is to strictly improve on it.
"""

_FILE_CHAR_LIMIT = 8_000    # max characters injected per individual file
_TOTAL_CHAR_LIMIT = 60_000  # max characters for the entire file_context block
_TAIL_LINES = 120           # when a file is truncated, keep this many tail lines
                            # (tail is usually more relevant than the top for large files)


def _truncate_file_content(content: str, path_str: str) -> tuple[str, bool]:
    """Return (truncated_content, was_truncated).

    Strategy: if the file exceeds _FILE_CHAR_LIMIT, keep the first half and
    last _TAIL_LINES lines so that both the module header and the most-recently-
    edited code at the bottom are visible.
    """
    if len(content) <= _FILE_CHAR_LIMIT:
        return content, False

    lines = content.splitlines(keepends=True)
    # Always show at least the last _TAIL_LINES lines
    tail = lines[-_TAIL_LINES:] if len(lines) > _TAIL_LINES else lines
    tail_text = "".join(tail)

    # Fill remaining budget from the top
    budget = _FILE_CHAR_LIMIT - len(tail_text)
    if budget > 0:
        head_text = content[:budget]
        # Don't cut in the middle of a line
        last_nl = head_text.rfind("\n")
        head_text = head_text[: last_nl + 1] if last_nl >= 0 else head_text
        omitted = len(lines) - head_text.count("\n") - _TAIL_LINES
        truncated = (
            head_text
            + f"\n... [{omitted} lines omitted — file truncated to fit context] ...\n\n"
            + tail_text
        )
    else:
        # File is so large that even the tail fills the budget — just show tail
        truncated = (
            f"... [file truncated — showing last {_TAIL_LINES} lines] ...\n\n"
            + tail_text
        )

    return truncated, True


def _read_source_files(workspace: str, glob_patterns: list[str]) -> str:
    """Read and concatenate source files matching glob patterns.

    Per-file limit: _FILE_CHAR_LIMIT chars (head + tail strategy).
    Total limit: _TOTAL_CHAR_LIMIT chars across all files.
    Files are sorted by modification time (most-recently-changed first) so
    that the most relevant files are included when the total budget is tight.
    """
    import glob as glob_mod

    # Collect all matching files with their mtime for sorting.
    # Use the resolved absolute path as the dedup key so that two glob patterns
    # that overlap (e.g. "**/*.py" and "src/**/*.py") never inject the same
    # file twice.  Without dedup the file would be read twice, counted against
    # the _TOTAL_CHAR_LIMIT twice, and sent to the LLM as duplicate context —
    # bumping other files out of the budget silently.
    seen_resolved: set[str] = set()
    file_entries: list[tuple[float, str]] = []  # (mtime, rel_path)
    for pattern in glob_patterns:
        for path_str in glob_mod.glob(pattern, recursive=True, root_dir=workspace):
            full = Path(workspace) / path_str
            if not full.is_file():
                continue
            resolved = str(full.resolve())
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            try:
                mtime = full.stat().st_mtime
                file_entries.append((mtime, path_str))
            except OSError:
                continue

    # Most recently modified first — most likely to be relevant
    file_entries.sort(key=lambda e: e[0], reverse=True)

    parts: list[str] = []
    total_chars = 0
    truncated_files: list[str] = []
    skipped_files: list[str] = []

    for _mtime, path_str in file_entries:
        if total_chars >= _TOTAL_CHAR_LIMIT:
            skipped_files.append(path_str)
            continue

        full = Path(workspace) / path_str
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        content, was_truncated = _truncate_file_content(content, path_str)
        if was_truncated:
            truncated_files.append(path_str)

        # Check total budget after per-file truncation
        remaining = _TOTAL_CHAR_LIMIT - total_chars
        if len(content) > remaining:
            content = content[:remaining]
            skipped_files.append(path_str)  # partially included

        block = f"=== FILE: {path_str} ===\n{content}\n"
        parts.append(block)
        total_chars += len(block)

    if not parts:
        return "[No source files matched]\n"

    # Append a concise manifest so the LLM knows what it's missing
    if truncated_files or skipped_files:
        notes: list[str] = []
        if truncated_files:
            notes.append(
                f"[Truncated to fit context: {', '.join(truncated_files)}]"
            )
        if skipped_files:
            notes.append(
                f"[Omitted (total context limit): {', '.join(skipped_files)}]"
            )
        parts.append("\n".join(notes) + "\n")

    return "".join(parts)


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
        log.info(
            "R%d/phase=%s/inner=%d: debating (prompt=%d chars)",
            outer + 1, phase.label, inner + 1, len(prompt),
        )

        # Use the phase's own system_prompt as the debate system instruction when
        # it is a plain string (not a Template with $variables) — i.e. the config
        # author set a dedicated system prompt for the proposer role.  Fall back to
        # a sensible default only when phase.system_prompt is empty or still
        # contains un-substituted Template variables (which would be confusing as a
        # system prompt).
        _debate_system = _DEBATE_SYSTEM_DEFAULT
        if phase.system_prompt and "$" not in phase.system_prompt:
            _debate_system = phase.system_prompt

        resp = await self.llm.call(
            [{"role": "user", "content": prompt}],
            system=_debate_system,
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
            "R%d/phase=%s/inner=%d: done  proposal=%d chars  "
            "basic=%.1f diffusion=%.1f combined=%.1f",
            outer + 1, phase.label, inner + 1, len(proposal),
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
        log.info(
            "R%d/phase=%s/inner=%d: implementing (prompt=%d chars)",
            outer + 1, phase.label, inner + 1, len(prompt),
        )

        text, exec_log = await self.llm.call_with_tools(
            [{"role": "user", "content": prompt}],
            self.registry,
            system=EXECUTOR_SYSTEM,
        )
        log.info(
            "R%d/phase=%s/inner=%d: tool_loop done  tool_calls=%d",
            outer + 1, phase.label, inner + 1, len(exec_log),
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
            "R%d/phase=%s/inner=%d: done  "
            "basic=%.1f diffusion=%.1f combined=%.1f  syntax=%s",
            outer + 1, phase.label, inner + 1,
            dual_score.basic.score, dual_score.diffusion.score,
            dual_score.combined,
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

        # Build round data.
        # Budget: divide a fixed total allowance evenly across rounds so that
        # a phase with 1 inner round gets a full 6 000-char proposal window
        # while a phase with 6 inner rounds still fits within ~36 000 chars.
        # Critique budget is 50 % of proposal budget (each evaluator gets 25 %).
        _TOTAL_ROUND_BUDGET = 18_000   # chars for all proposals across all rounds
        _TOTAL_CRIT_BUDGET  =  9_000   # chars for all critiques across all rounds
        n_rounds = max(len(results), 1)
        _proposal_cap = _TOTAL_ROUND_BUDGET // n_rounds
        _critique_cap = _TOTAL_CRIT_BUDGET  // (n_rounds * 2)  # per evaluator
        round_parts: list[str] = []
        for i, r in enumerate(results):
            score_info = f"combined={r.combined_score:.1f}"
            if r.dual_score:
                score_info = f"basic={r.dual_score.basic.score:.1f}, diffusion={r.dual_score.diffusion.score:.1f}"
            round_parts.append(
                f"=== Round {i + 1} ({score_info}) ===\n"
                f"{r.proposal[:_proposal_cap]}\n"
            )
            if r.dual_score:
                round_parts.append(
                    f"--- Basic Evaluator ---\n{r.dual_score.basic.critique[:_critique_cap]}\n"
                    f"--- Diffusion Evaluator ---\n{r.dual_score.diffusion.critique[:_critique_cap]}\n"
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
        """Build the executor prompt from the phase template.

        Variable substitutions available in ``phase.system_prompt``:

        * ``$file_context``           — concatenated source files
        * ``$prior_best``             — best result from previous round/inner (with header)
        * ``$syntax_errors``          — syntax error block (with header) if any
        * ``$falsifiable_criterion``  — phase's falsifiable criterion string

        Warns when ``$file_context`` is absent from the template while
        ``glob_patterns`` are configured — that combination means the injected
        source files are silently discarded, which is almost always a bug.
        """
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

        template = phase.system_prompt
        if phase.glob_patterns and "$file_context" not in template:
            log.warning(
                "Phase %s: system_prompt template does not contain $file_context "
                "but glob_patterns=%r are configured — source files will NOT be "
                "injected into the prompt (add $file_context to the template or "
                "remove glob_patterns to silence this warning)",
                phase.label,
                phase.glob_patterns,
            )

        return Template(template).safe_substitute(
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
