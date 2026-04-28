"""Evaluation orchestration for the agent loop.

Wraps ``DualEvaluator`` calls, score formatting, and periodic checkpoint
into free functions so ``agent_loop.py`` stays pure orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.core.llm import LLM
from harness.evaluation.dual_evaluator import DualEvaluator, DualScore
from harness.prompts import agent_meta_review as meta_review_prompts
from harness.prompts import agent_squash as squash_prompts
from harness.agent import agent_git

log = logging.getLogger(__name__)

# Maximum score history entries to keep in memory.
_MAX_SCORE_HISTORY = 50


@dataclass
class MetaReviewDecision:
    """Structured decision from the meta-review LLM (US-10)."""
    action: str           # "continue" | "pivot" | "stop"
    reason: str
    pivot_direction: str  # meaningful only when action == "pivot"


@dataclass
class CheckpointResult:
    """Result of a periodic checkpoint (startup or in-loop)."""
    meta_context: str    # strategic direction text injected into system prompt
    head_hash: str       # HEAD hash after checkpoint (squash may change SHAs)
    squashed: bool       # whether squash was executed
    tagged: str          # tag name, empty if not tagged
    decision: MetaReviewDecision | None = None  # US-10: structured meta-review decision


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

async def run_evaluation(
    evaluator: DualEvaluator | None,
    cycle: int,
    diff_text: str,
    mission: str,
    *,
    has_diff: bool = True,
) -> DualScore | None:
    """Run DualEvaluator on a cycle's deliverable.

    When *has_diff* is True (default), evaluates code changes in ``implement``
    mode.  When False, evaluates the agent's reasoning/exploration output in
    ``reasoning`` mode.  Returns the score or ``None``.
    """
    if evaluator is None:
        return None
    if not diff_text.strip():
        diff_text = "(empty cycle — agent produced no output)"
    try:
        mission_ctx = mission[:200] if mission else "autonomous maintenance"
        mode = "implement" if has_diff else "reasoning"
        context_label = "code changes" if has_diff else "agent reasoning (no code changes)"
        score = await evaluator.evaluate(
            subject=diff_text,
            context=f"Mission: {mission_ctx}\nAgent cycle {cycle + 1} {context_label}.",
            mode=mode,
        )
        log.info(
            "agent_eval: cycle %d — basic=%.1f diffusion=%.1f combined=%.1f",
            cycle + 1, score.basic.score, score.diffusion.score, score.combined,
        )
        return score
    except Exception as exc:
        log.warning("agent_eval: evaluation failed for cycle %d: %s", cycle + 1, exc)
        return None


def record_score(
    score: DualScore,
    cycle: int,
    score_history: list[dict[str, Any]],
) -> None:
    """Append a score entry to *score_history*, enforcing the cap."""
    score_history.append({
        "cycle": cycle + 1,
        "basic": score.basic.score,
        "diffusion": score.diffusion.score,
        "combined": score.combined,
    })
    if len(score_history) > _MAX_SCORE_HISTORY:
        score_history.pop(0)


def persist_eval_scores(
    score: DualScore,
    cycle: int,
    write_fn: Callable[..., Any],
) -> None:
    """Write eval scores JSON to artifacts."""
    try:
        write_fn(
            json.dumps({
                "basic": score.basic.score,
                "diffusion": score.diffusion.score,
                "combined": score.combined,
                "basic_critique": score.basic.critique[:500],
                "diffusion_critique": score.diffusion.critique[:500],
            }, indent=2),
            f"cycle_{cycle + 1}", "eval_scores.json",
        )
    except Exception as exc:
        log.warning("agent_eval: failed to persist eval scores for cycle %d: %s", cycle + 1, exc)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_eval_oneliner(score: DualScore) -> str:
    """One-line summary for git commit messages (no critique)."""
    return (
        f"basic={score.basic.score:.1f} "
        f"diffusion={score.diffusion.score:.1f} "
        f"combined={score.combined:.1f}"
    )


def format_eval_notes(score: DualScore) -> str:
    """Format evaluation scores as a compact string for agent_notes.md."""
    lines = [
        f"[eval] basic={score.basic.score:.1f} diffusion={score.diffusion.score:.1f} "
        f"combined={score.combined:.1f}",
    ]
    if score.basic.critique:
        lines.append(f"  basic critique: {score.basic.critique[:200]}")
    if score.diffusion.critique:
        lines.append(f"  diffusion critique: {score.diffusion.critique[:200]}")
    return "\n".join(lines)


def format_score_history(score_history: list[dict[str, Any]]) -> str:
    """Format score history as a markdown table for meta-review input."""
    if not score_history:
        return "(no scores recorded yet)"
    lines = [
        "| Cycle | Basic | Diffusion | Combined |",
        "|-------|-------|-----------|----------|",
    ]
    for entry in score_history[-20:]:
        lines.append(
            f"| {entry['cycle']} | {entry['basic']:.1f} | "
            f"{entry['diffusion']:.1f} | {entry['combined']:.1f} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checkpoint — unified periodic orchestration
# ---------------------------------------------------------------------------

async def _meta_review_llm(
    llm: LLM,
    score_table: str,
    git_delta: str,
    notes: str,
    coverage_report: str = "",
) -> tuple[str, MetaReviewDecision | None]:
    """Run the meta-review LLM call (pure analysis, no side effects).

    Returns (free_text, decision).  The decision is parsed from a JSON
    block in the LLM output; if parsing fails, decision is None.
    """
    user_content = meta_review_prompts.AGENT_META_REVIEW_USER
    user_content = user_content.replace("$score_history", score_table)
    user_content = user_content.replace("$git_delta", git_delta)
    user_content = user_content.replace(
        "$coverage_report", coverage_report or "(no coverage data yet)"
    )
    user_content = user_content.replace("$current_notes", notes)

    response = await llm.call(
        [{"role": "user", "content": user_content}],
        system=meta_review_prompts.AGENT_META_REVIEW_SYSTEM,
    )
    raw = (response.text or "").strip()
    free_text, decision = _parse_meta_review_decision(raw)
    return free_text, decision


_VALID_ACTIONS = frozenset({"continue", "pivot", "stop"})


def _parse_meta_review_decision(
    raw_text: str,
) -> tuple[str, MetaReviewDecision | None]:
    """Extract structured decision from meta-review LLM output (US-10).

    Looks for a ```json ... ``` fenced block containing an object with an
    ``action`` field.  Returns (free_text_without_json_block, decision).
    On any parse failure, returns (original_text, None) — the caller
    treats None as "continue" (backward compatible).
    """
    # Find ```json ... ``` block
    pattern = r"```json\s*\n(\{[^`]*?\})\s*\n```"
    match = re.search(pattern, raw_text, re.DOTALL)
    if not match:
        log.debug("meta-review decision: no JSON block found, defaulting to continue")
        return raw_text, None

    json_str = match.group(1)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        log.warning("meta-review decision: JSON parse error: %s", exc)
        return raw_text, None

    action = data.get("action", "")
    if action not in _VALID_ACTIONS:
        log.warning("meta-review decision: invalid action %r, ignoring", action)
        return raw_text, None

    reason = data.get("reason", "")
    pivot_direction = data.get("pivot_direction", "")

    if action == "pivot" and not pivot_direction:
        log.warning("meta-review decision: pivot without pivot_direction, treating as continue")
        return raw_text, MetaReviewDecision(
            action="continue",
            reason="pivot requested but no direction provided",
            pivot_direction="",
        )

    # Strip the JSON block from the free-text output
    free_text = raw_text[:match.start()].rstrip() + raw_text[match.end():].lstrip()

    return free_text.strip(), MetaReviewDecision(
        action=action,
        reason=reason,
        pivot_direction=pivot_direction,
    )


async def _squash_grouping_llm(
    llm: LLM,
    commits: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Run the squash grouping LLM call and parse the result.

    Returns validated groups or None if the LLM says no squash is needed.
    """
    from harness.agent.agent_squash import _build_commit_list, _parse_groups

    commit_list = _build_commit_list(commits)
    user_content = squash_prompts.SQUASH_GROUPING_USER.replace(
        "$commit_list", commit_list,
    )

    response = await llm.call(
        [{"role": "user", "content": user_content}],
        system=squash_prompts.SQUASH_GROUPING_SYSTEM,
        max_tokens=2000,
    )

    groups = _parse_groups(response.text or "", commits)
    if groups is None:
        return None

    # Filter out single-commit groups (nothing to squash)
    needs_squash = [g for g in groups if len(g["shas"]) > 1]
    if not needs_squash:
        log.info("checkpoint: LLM says all commits are independent, no squash needed")
        return None

    return groups


async def _compress_notes_llm(
    llm: LLM,
    old_notes: str,
) -> str:
    """Compress old cycle notes into a concise summary via LLM.

    Returns the compressed text, or empty string on failure.
    """
    user_content = meta_review_prompts.NOTES_COMPRESS_USER.replace(
        "$old_notes", old_notes,
    )
    response = await llm.call(
        [{"role": "user", "content": user_content}],
        system=meta_review_prompts.NOTES_COMPRESS_SYSTEM,
        max_tokens=2000,
    )
    return (response.text or "").strip()


# How many recent cycle note blocks to keep uncompressed.
# Older blocks get compressed by the LLM at checkpoint time.
_RECENT_NOTES_KEEP = 10
# Minimum number of old note blocks before compression is triggered.
_COMPRESS_THRESHOLD = 5


async def _tag_checkpoint(
    repo_path: Path,
    cycle: int,
    prefix: str,
    push_remote: str,
    push_tag: bool,
) -> str:
    """Create a tag at HEAD for this checkpoint.  Returns tag name or ""."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short=7", "HEAD",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sha_out, _ = await proc.communicate()
        if proc.returncode != 0:
            return ""
        short_sha = sha_out.decode().strip()
        tag_name = f"{prefix}-{cycle + 1}-{short_sha}"

        tag_proc = await asyncio.create_subprocess_exec(
            "git", "tag", "-f", tag_name,
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, tag_err = await tag_proc.communicate()
        if tag_proc.returncode != 0:
            log.warning("checkpoint: tag create failed: %s", tag_err.decode(errors="replace")[:200])
            return ""

        log.info("checkpoint: created tag %r (cycle=%d)", tag_name, cycle + 1)

        if push_tag:
            push_proc = await asyncio.create_subprocess_exec(
                "git", "push", push_remote, tag_name,
                cwd=str(repo_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, push_err = await push_proc.communicate()
            if push_proc.returncode == 0:
                log.info("checkpoint: pushed tag %r to %s", tag_name, push_remote)
            else:
                log.warning("checkpoint: tag push failed: %s", push_err.decode(errors="replace")[:200])

        return tag_name
    except Exception as exc:
        log.warning("checkpoint: tag error: %s", exc)
        return ""


async def run_checkpoint(
    llm: LLM,
    cycle: int,
    score_history: list[dict[str, Any]],
    since_hash: str,
    current_notes: str,
    repo_path: Path,
    write_fn: Callable[..., Any],
    *,
    notes_path: Path | None = None,
    auto_squash: bool = False,
    auto_tag: bool = False,
    tag_prefix: str = "harness-r",
    tag_push: bool = True,
    push_remote: str = "origin",
    coverage_report: str = "",
) -> CheckpointResult:
    """Run the periodic checkpoint: strategic review + maintenance actions.

    Called at startup (*cycle* = -1, no squash/tag) and every N cycles.
    Up to three LLM calls run in parallel:

      1. Meta-review (strategic direction)
      2. Squash grouping (commit analysis)     — if ``auto_squash``
      3. Notes compression (old notes → summary) — if enough old notes

    Execution actions (squash → tag) run sequentially with dependencies.

    Returns a ``CheckpointResult`` with strategic direction, final HEAD
    hash, and action outcomes.  On failure, returns empty context so the
    caller can safely proceed.
    """
    label = "startup" if cycle < 0 else f"cycle {cycle + 1}"
    log.info("checkpoint: running %s checkpoint", label)

    # ── 1. Prepare inputs ──
    score_table = format_score_history(score_history)
    git_delta = await agent_git.get_review_git_delta(
        repo_path, since_hash or "HEAD~20",
    )

    # Split notes into old (compressible) and recent (keep as-is).
    all_parts = re.split(r"(?=^## (?:Cycle \d+|Compressed History))", current_notes, flags=re.MULTILINE)
    all_parts = [p for p in all_parts if p.strip()]
    if len(all_parts) > _RECENT_NOTES_KEEP:
        old_parts = all_parts[:-_RECENT_NOTES_KEEP]
        recent_parts = all_parts[-_RECENT_NOTES_KEEP:]
    else:
        old_parts = []
        recent_parts = all_parts

    # For meta-review, truncate if needed
    notes_for_review = current_notes
    if len(notes_for_review) > 3000:
        log.debug("checkpoint: notes truncated from %d to 3k chars for review", len(notes_for_review))
        notes_for_review = notes_for_review[-3000:]

    commits: list[dict[str, str]] = []
    if auto_squash:
        commits = await agent_git.get_commits_since(repo_path, since_hash)

    # ── 2. Parallel LLM calls ──
    # Index tracking: 0=meta-review, 1+=optional (squash, compress)
    coros: list[Any] = [_meta_review_llm(llm, score_table, git_delta, notes_for_review, coverage_report)]
    squash_idx = -1
    compress_idx = -1

    if auto_squash and commits:
        squash_idx = len(coros)
        coros.append(_squash_grouping_llm(llm, commits))

    if len(old_parts) >= _COMPRESS_THRESHOLD and notes_path is not None:
        compress_idx = len(coros)
        old_text = "".join(old_parts)
        coros.append(_compress_notes_llm(llm, old_text))

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Extract meta-review result (always index 0) — now a (text, decision) tuple
    decision: MetaReviewDecision | None = None
    if isinstance(results[0], Exception):
        log.warning("checkpoint: meta-review LLM failed: %s", results[0])
        meta_context = ""
    else:
        meta_context, decision = results[0]

    # Extract squash groups (if requested)
    squash_groups = None
    if squash_idx >= 0:
        if isinstance(results[squash_idx], Exception):
            log.warning("checkpoint: squash grouping LLM failed: %s", results[squash_idx])
        else:
            squash_groups = results[squash_idx]

    # Extract compressed notes (if requested)
    compressed_notes = None
    if compress_idx >= 0:
        if isinstance(results[compress_idx], Exception):
            log.warning("checkpoint: notes compression failed: %s", results[compress_idx])
        else:
            compressed_notes = results[compress_idx]

    # ── 3. Sequential execution (dependencies: squash → tag) ──
    squashed = False
    if squash_groups is not None:
        log.info(
            "checkpoint: squashing %d commits into %d groups",
            sum(len(g["shas"]) for g in squash_groups),
            len(squash_groups),
        )
        squashed = await agent_git.squash_groups(repo_path, since_hash, squash_groups)

    head_hash = await agent_git.get_head_hash(repo_path)

    tag_name = ""
    if auto_tag and cycle >= 0:
        tag_name = await _tag_checkpoint(
            repo_path, cycle, tag_prefix, push_remote, tag_push,
        )

    # ── 4. Write compressed notes ──
    if compressed_notes and notes_path is not None:
        try:
            recent_text = "".join(recent_parts)
            new_content = compressed_notes.strip() + "\n\n" + recent_text.strip() + "\n"
            notes_path.write_text(new_content, encoding="utf-8")
            log.info(
                "checkpoint: compressed %d old note blocks → %d chars, kept %d recent",
                len(old_parts), len(compressed_notes), len(recent_parts),
            )
        except OSError as exc:
            log.warning("checkpoint: failed to write compressed notes: %s", exc)

    # ── 5. Persist checkpoint artifact ──
    artifact_label = "startup" if cycle < 0 else f"cycle_{cycle + 1}"
    try:
        write_fn(meta_context, artifact_label, "checkpoint.md")
    except Exception:
        pass

    log.info(
        "checkpoint: %s complete — direction=%d chars, squashed=%s, tag=%s",
        label, len(meta_context), squashed, tag_name or "(none)",
    )

    if decision:
        log.info(
            "checkpoint: meta-review decision: action=%s reason=%s",
            decision.action, decision.reason[:80],
        )

    return CheckpointResult(
        meta_context=meta_context,
        head_hash=head_hash,
        squashed=squashed,
        tagged=tag_name,
        decision=decision,
    )
