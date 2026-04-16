"""PhaseRunner — executes a single phase: inner rounds → synthesis → hooks."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from string import Template

from harness.artifacts import ArtifactStore
from harness.checkpoint import CheckpointManager
from harness.core.config import PipelineConfig
from harness.evaluation.dual_evaluator import DualEvaluator
from harness.executor import executor_system_with_workspace
from harness.hooks import build_hooks
from harness.core.llm import LLM
from harness.pipeline.phase import DualScore, InnerResult, PhaseConfig, PhaseResult, ScoreItem
from harness.prompts import synthesis as synth_prompts
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MIN_SYNTHESIS_CHARS = 150

# Default system prompt used in debate-mode rounds when a phase has no plain
# (non-template) system_prompt.  A concrete, opinionated default gets better
# proposals than a generic "You are a senior software engineer" stub.
_DEBATE_SYSTEM_DEFAULT = """\
You are a senior software engineer producing a detailed, actionable
implementation proposal.

REQUIREMENTS — your proposal MUST satisfy all of the following:
1. REFERENCE SPECIFIC code entities: name the exact function, class, method,
   and file path from the source context.  Generic descriptions such as
   "update the helper" or "fix the loop" score 0 on specificity.
2. COVER EVERY stated requirement.  Cross-reference each clause of the
   falsifiable criterion against your proposal line by line.
3. STRUCTURE each change as:
     FILE: <path>
     CHANGE: <precise description of what is modified and why>
     EDGE CASE: <at least one failure mode and how your design handles it>
4. SELF-CONSISTENCY CHECK — before writing, verify:
   a. Do later steps depend on symbols created in earlier steps in the correct
      order? (No forward references to undefined names.)
   b. Does any step modify a public API?  List all call sites that must be
      updated in the same proposal.
5. IMPROVE ON THE PRIOR BEST — if a prior best is provided, identify its
   single most important defect and fix it.  Do not repeat it verbatim.

ANTI-PATTERNS that guarantee a low score:
- "Update X to handle Y" without naming X's exact file and function
- Adding a new class when augmenting an existing one would suffice
- Plans that work only for the happy path (no error handling)
- Vague rationale: "for better performance" without a measurement
"""

_FILE_CHAR_LIMIT = 8_000    # max characters injected per individual file
_TOTAL_CHAR_LIMIT = 60_000  # max characters for the entire file_context block
_TAIL_LINES = 120           # when a file is truncated, keep this many tail lines
                            # (tail is usually more relevant than the top for large files)

# ---------------------------------------------------------------------------
# File relevance scoring
# ---------------------------------------------------------------------------
# Weights for the composite relevance score (must sum to 1.0).
_W_KEYWORD  = 0.50   # keyword overlap between file path and phase keywords
_W_RECENCY  = 0.35   # how recently the file was modified
_W_SIZE     = 0.15   # inverse-size signal (prefer smaller files when keywords equal)

# Recency half-life: files modified within this window score near 1.0 on
# recency; older files decay exponentially.  24 h is a good default for
# active development workflows.
_RECENCY_HALF_LIFE_SECS: float = 24 * 3600   # 24 hours


def _tokenise_path(path_str: str) -> set[str]:
    """Split a file path into lowercase tokens for keyword matching.

    Splits on directory separators, underscores, hyphens, dots, and
    camelCase/PascalCase boundaries so that e.g.
    ``"src/phase_runner.py"`` → ``{"src", "phase", "runner", "py"}``
    and ``"harness/DualEvaluator.py"`` → ``{"harness", "dual", "evaluator", "py"}``.
    """
    # Replace separators and punctuation with spaces
    raw = re.sub(r"[/\._\-]", " ", path_str)
    # Split on camelCase / PascalCase boundaries
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    return {t.lower() for t in raw.split() if t}


def _tokenise_phrase(phrase: str) -> set[str]:
    """Tokenise a phase name or task description into lowercase keywords.

    Strips common English stop-words so that ``"requirements analysis"``
    becomes ``{"requirements", "analysis"}`` rather than including ``"a"``,
    ``"the"``, ``"for"``, etc., which would match nearly every file.
    """
    _STOP_WORDS = frozenset({
        "a", "an", "the", "and", "or", "of", "for", "in", "on", "at",
        "to", "is", "it", "be", "as", "by", "we", "do", "up", "so",
        "all", "new", "add", "use", "run", "get", "set", "with",
    })
    raw = re.sub(r"[^a-zA-Z0-9 _\-]", " ", phrase)
    return {
        t.lower()
        for t in re.split(r"[\s_\-]+", raw)
        if t and t.lower() not in _STOP_WORDS and len(t) > 1
    }


def score_file_relevance(
    path_str: str,
    keywords: set[str],
    mtime: float,
    now: float,
    file_size: int = 0,
) -> float:
    """Compute a composite relevance score in [0, 1] for a source file.

    Higher scores → file appears earlier in context injection.

    Scoring signals
    ---------------
    **Keyword overlap** (weight 50%): Jaccard similarity between the set of
    tokens in the file path and the set of ``keywords`` extracted from the
    phase name / task description.  A file whose name or directory directly
    mentions the phase's domain scores close to 1.0 on this dimension.

    **Recency** (weight 35%): Exponential decay based on time since last
    modification, with a half-life of ``_RECENCY_HALF_LIFE_SECS`` (24 h).
    A file edited in the last hour scores ~1.0; one edited a week ago scores
    ~0.06.

    **Inverse size** (weight 15%): Files smaller than ``_FILE_CHAR_LIMIT``
    get a small bonus over very large files.  This prevents a single enormous
    file from crowding out several smaller, equally relevant files.

    Args:
        path_str:  Relative file path (e.g. ``"harness/phase_runner.py"``).
        keywords:  Set of lowercase keyword tokens from the phase/task.
        mtime:     File modification time as a Unix timestamp.
        now:       Current time as a Unix timestamp (for recency calculation).
        file_size: File size in bytes (0 = unknown, skips size signal).

    Returns:
        Composite score in [0.0, 1.0].
    """
    # --- keyword signal ---
    path_tokens = _tokenise_path(path_str)
    if keywords and path_tokens:
        # Jaccard index: |intersection| / |union|
        intersection = len(path_tokens & keywords)
        union = len(path_tokens | keywords)
        keyword_score = intersection / union if union > 0 else 0.0
    else:
        keyword_score = 0.0

    # --- recency signal ---
    age_secs = max(0.0, now - mtime)
    # Exponential decay: score = 0.5^(age / half_life)
    recency_score = math.pow(0.5, age_secs / _RECENCY_HALF_LIFE_SECS)

    # --- inverse-size signal ---
    if file_size > 0:
        # Files at or below _FILE_CHAR_LIMIT score 1.0; larger files decay
        # logarithmically (not linearly) so a 2× oversize file still gets ~0.7).
        if file_size <= _FILE_CHAR_LIMIT:
            size_score = 1.0
        else:
            # log₂(limit/size): negative for oversized files → clamp to [0,1]
            ratio = _FILE_CHAR_LIMIT / file_size
            size_score = max(0.0, min(1.0, 1.0 + math.log2(ratio)))
    else:
        size_score = 0.5  # neutral when size is unknown

    return (
        _W_KEYWORD * keyword_score
        + _W_RECENCY * recency_score
        + _W_SIZE    * size_score
    )


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


def _read_source_files(
    workspace: str,
    glob_patterns: list[str],
    keywords: set[str] | None = None,
    total_char_limit: int = _TOTAL_CHAR_LIMIT,
) -> str:
    """Read and concatenate source files matching glob patterns.

    Per-file limit: _FILE_CHAR_LIMIT chars (head + tail strategy).
    Total limit: _TOTAL_CHAR_LIMIT chars across all files.

    Ordering: when ``keywords`` are provided (extracted from the phase name
    or task description), files are ranked by a composite relevance score
    — keyword overlap × path tokens + recency decay + inverse-size signal —
    so that the most task-relevant files are always injected first and fill
    the context budget before less-relevant ones.  When ``keywords`` is
    ``None`` or empty the function falls back to pure mtime ordering
    (most-recently-changed first), preserving prior behaviour.

    Args:
        workspace:     Absolute path to the project workspace.
        glob_patterns: List of glob patterns relative to ``workspace``.
        keywords:      Optional set of lowercase keyword tokens from the
                       phase name / task (see ``_tokenise_phrase``).  Pass
                       ``None`` to use pure mtime ordering.
    """
    import glob as glob_mod

    # Collect all matching files with their mtime and size for scoring.
    # Use the resolved absolute path as the dedup key so that two glob patterns
    # that overlap (e.g. "**/*.py" and "src/**/*.py") never inject the same
    # file twice.  Without dedup the file would be read twice, counted against
    # the _TOTAL_CHAR_LIMIT twice, and sent to the LLM as duplicate context —
    # bumping other files out of the budget silently.
    seen_resolved: set[str] = set()
    # (mtime, size_bytes, rel_path)
    file_entries: list[tuple[float, int, str]] = []
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
                st = full.stat()
                file_entries.append((st.st_mtime, st.st_size, path_str))
            except OSError:
                continue

    if not file_entries:
        return "[No source files matched]\n"

    # Sort by composite relevance score (descending) when keywords are provided;
    # fall back to pure mtime (descending) otherwise.
    if keywords:
        now = time.time()
        file_entries.sort(
            key=lambda e: score_file_relevance(
                e[2],           # rel_path
                keywords,
                e[0],           # mtime
                now,
                file_size=e[1], # bytes
            ),
            reverse=True,
        )
        log.debug(
            "_read_source_files: ranking %d files by relevance (keywords=%s)",
            len(file_entries),
            sorted(keywords)[:8],   # cap log length
        )
    else:
        # Legacy: most recently modified first
        file_entries.sort(key=lambda e: e[0], reverse=True)

    parts: list[str] = []
    total_chars = 0
    truncated_files: list[str] = []
    skipped_files: list[str] = []

    for _mtime, _size, path_str in file_entries:
        if total_chars >= total_char_limit:
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
        remaining = total_char_limit - total_chars
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

        # 1. Read source files once for the whole phase.
        # Extract relevance keywords from the phase name and falsifiable
        # criterion so that the most task-relevant files are ranked highest
        # when the total context budget is constrained.
        phase_keywords = _tokenise_phrase(
            f"{phase.name} {phase.falsifiable_criterion}"
        )
        file_context = _read_source_files(
            self.harness.workspace,
            phase.glob_patterns,
            keywords=phase_keywords or None,
            total_char_limit=self.config.max_file_context_chars,
        )
        log.info(
            "Phase %s: injected %d chars from source files  (keywords=%s)",
            label,
            len(file_context),
            sorted(phase_keywords)[:6] if phase_keywords else "[]",
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
            hooks = build_hooks(phase, pipeline_config=self.config)
            # Build changes summary from tool call logs of the best inner round.
            changes_summary = ""
            if best_result and best_result.tool_call_log:
                file_tools = {"write_file", "edit_file", "file_patch", "find_replace"}
                changed = [
                    e["tool"] for e in best_result.tool_call_log
                    if e["tool"] in file_tools and e.get("success", True)
                ]
                if changed:
                    changes_summary = f"{len(changed)} file edit(s)"
            for hook in hooks:
                ctx = {
                    "outer": outer,
                    "phase_name": phase.name,
                    "best_score": best_result.combined_score if best_result else 0.0,
                    "changes_summary": changes_summary,
                }
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
        # it is a plain string (not a Template with $variables).  Fall back to
        # a sensible default when empty or contains un-substituted Template vars.
        _debate_system = _DEBATE_SYSTEM_DEFAULT
        if phase.system_prompt and "$" not in phase.system_prompt:
            _debate_system = phase.system_prompt

        resp = await self.llm.call(
            [{"role": "user", "content": prompt}],
            system=_debate_system,
        )
        proposal = resp.text
        self.artifacts.write(proposal, *segs, "proposal.txt")

        # Skip evaluation for short proposals (saves 2 API calls).
        if phase.min_proposal_chars and len(proposal.strip()) < phase.min_proposal_chars:
            log.info(
                "R%d/phase=%s/inner=%d: proposal too short (%d < %d chars), skipping eval",
                outer + 1, phase.label, inner + 1,
                len(proposal.strip()), phase.min_proposal_chars,
            )
            dual_score = DualScore(
                basic=ScoreItem(0.0, "[skipped — proposal below min_proposal_chars]"),
                diffusion=ScoreItem(0.0, "[skipped — proposal below min_proposal_chars]"),
            )
            return InnerResult(proposal=proposal, dual_score=dual_score)

        dual_score = await self._evaluate_and_log(
            outer, phase, inner, proposal, file_context, segs,
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

        _impl_t0 = time.monotonic()
        # Dynamic tool filtering: if the phase specifies tool_tags, only
        # expose matching tools to the executor LLM.
        active_registry = self.registry
        if phase.tool_tags:
            active_registry = self.registry.filter_by_tags(frozenset(phase.tool_tags))
            log.info(
                "R%d/phase=%s/inner=%d: filtered tools by tags %s → %d tools",
                outer + 1, phase.label, inner + 1,
                phase.tool_tags, len(active_registry.names),
            )

        text, exec_log = await self.llm.call_with_tools(
            [{"role": "user", "content": prompt}],
            active_registry,
            system=executor_system_with_workspace(self.harness.workspace),
            max_turns=self.harness.max_tool_turns,
        )
        _impl_elapsed = time.monotonic() - _impl_t0
        log.info(
            "R%d/phase=%s/inner=%d: tool_loop done  tool_calls=%d  elapsed=%.1fs",
            outer + 1, phase.label, inner + 1, len(exec_log), _impl_elapsed,
        )
        implement_log = "\n".join(
            f"- {e['tool']}: {e['output'][:200]}" for e in exec_log
        )
        self.artifacts.write(implement_log, *segs, "implement_output.txt")

        # Build structured per-call records and write tool_metrics.json.
        # exec_log entries now carry "is_error" and "duration_ms" directly from
        # llm.call_with_tools (added in the traceability pass).  Fall back to
        # string-prefix heuristic for entries from older/custom LLM wrappers
        # that may not include these fields.
        _error_prefixes = ("SCHEMA ERROR", "PERMISSION ERROR", "TOOL ERROR")
        tool_call_records: list[dict] = []
        for e in exec_log:
            if "is_error" in e:
                success = not e["is_error"]
            else:
                out = e.get("output", "") or ""
                success = not any(out.startswith(pfx) for pfx in _error_prefixes)
            tool_call_records.append({
                "tool": e["tool"],
                "success": success,
                "duration_ms": e.get("duration_ms", 0),
            })
        tool_metrics_payload = {
            "round": outer + 1,
            "phase": phase.label,
            "inner_round": inner + 1,
            "tool_calls": tool_call_records,
            "total_calls": len(tool_call_records),
            "error_calls": sum(1 for r in tool_call_records if not r["success"]),
            "elapsed_s": round(_impl_elapsed, 2),
        }
        try:
            self.artifacts.write(
                json.dumps(tool_metrics_payload, indent=2),
                *segs, "tool_metrics.json",
            )
        except Exception as _e:
            log.warning("_run_implement_round: failed to write tool_metrics.json: %s", _e)

        # Re-read code state
        code_state = _read_source_files(
            self.harness.workspace, phase.glob_patterns,
            total_char_limit=self.config.max_file_context_chars,
        )
        self.artifacts.write(code_state, *segs, "post_impl_snapshot.txt")

        # Build eval subject from implementation log + code state.
        # Cap both sections so the combined text stays under 12 000 chars.
        _IMPL_LOG_CAP = 5_000
        _CODE_STATE_CAP = 7_000
        impl_log_capped = (
            implement_log[-_IMPL_LOG_CAP:] if len(implement_log) > _IMPL_LOG_CAP
            else implement_log
        )
        code_state_capped = code_state[:_CODE_STATE_CAP]
        eval_subject = (
            f"## Implementation Log\n\n{impl_log_capped}\n\n"
            f"## Code State After\n\n{code_state_capped}"
        )

        # Skip evaluation for short proposals (saves 2 API calls).
        if phase.min_proposal_chars and len(text.strip()) < phase.min_proposal_chars:
            log.info(
                "R%d/phase=%s/inner=%d: impl output too short (%d < %d chars), skipping eval",
                outer + 1, phase.label, inner + 1,
                len(text.strip()), phase.min_proposal_chars,
            )
            dual_score = DualScore(
                basic=ScoreItem(0.0, "[skipped — output below min_proposal_chars]"),
                diffusion=ScoreItem(0.0, "[skipped — output below min_proposal_chars]"),
            )
        else:
            dual_score = await self._evaluate_and_log(
                outer, phase, inner, eval_subject, file_context, segs,
            )

        # Syntax check (inline, not hook — needed for carry-forward)
        syntax_errors = ""
        if phase.syntax_check_patterns:
            from harness.hooks import SyntaxCheckHook
            hook = SyntaxCheckHook(phase.syntax_check_patterns)
            hook_result = await hook.run(self.harness, {})
            syntax_errors = hook_result.errors
            self.artifacts.write(syntax_errors, *segs, "syntax_errors.txt")
            if syntax_errors:
                log.info(
                    "R%d/phase=%s/inner=%d: syntax ERRORS detected",
                    outer + 1, phase.label, inner + 1,
                )

        return InnerResult(
            proposal=text,
            dual_score=dual_score,
            implement_log=implement_log,
            post_impl_snapshot=code_state,
            syntax_errors=syntax_errors,
            tool_call_log=tool_call_records,
        )

    async def _evaluate_and_log(
        self,
        outer: int,
        phase: PhaseConfig,
        inner: int,
        eval_subject: str,
        file_context: str,
        segs: tuple[str, ...],
    ) -> DualScore:
        """Run dual evaluation on *eval_subject* and write artifacts.

        Shared by both debate and implement rounds to avoid duplicating the
        evaluate → write-artifacts → log pattern.
        """
        dual_score = await self.dual_evaluator.evaluate(
            eval_subject, file_context,
            basic_system=self.config.dual_evaluator.basic_system,
            diffusion_system=self.config.dual_evaluator.diffusion_system,
        )
        self.artifacts.write(dual_score.basic.critique, *segs, "basic_eval.txt")
        self.artifacts.write(dual_score.diffusion.critique, *segs, "diffusion_eval.txt")

        log.info(
            "R%d/phase=%s/inner=%d: eval done  "
            "basic=%.1f diffusion=%.1f combined=%.1f",
            outer + 1, phase.label, inner + 1,
            dual_score.basic.score, dual_score.diffusion.score, dual_score.combined,
        )
        return dual_score

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

        rendered = Template(template).safe_substitute(
            file_context=file_context,
            prior_best=prior_section,
            syntax_errors=syntax_section,
            falsifiable_criterion=phase.falsifiable_criterion,
        )

        workspace_reminder = (
            f"**WORKSPACE**: `{self.harness.workspace}`"
            f" — all file paths must be under this directory.\n\n"
        )
        return workspace_reminder + rendered

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
        from harness.evaluation.dual_evaluator import parse_score

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
