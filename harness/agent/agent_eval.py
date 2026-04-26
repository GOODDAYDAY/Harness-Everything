"""Evaluation orchestration for the agent loop.

Wraps ``DualEvaluator`` calls, score formatting, and periodic meta-review
into free functions so ``agent_loop.py`` stays pure orchestration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.core.llm import LLM
from harness.evaluation.dual_evaluator import DualEvaluator, DualScore
from harness.prompts import agent_meta_review as meta_review_prompts
from harness.agent import agent_git

log = logging.getLogger(__name__)

# Maximum score history entries to keep in memory.
_MAX_SCORE_HISTORY = 50


@dataclass
class MetaReviewResult:
    """Result of a periodic meta-review."""
    context: str       # strategic direction text injected into system prompt
    head_hash: str     # HEAD hash at time of review (for next delta)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

async def run_evaluation(
    evaluator: DualEvaluator | None,
    cycle: int,
    diff_text: str,
    mission: str,
) -> DualScore | None:
    """Run DualEvaluator on a cycle's diff.  Returns the score or ``None``."""
    if evaluator is None or not diff_text.strip():
        return None
    try:
        mission_ctx = mission[:200] if mission else "autonomous maintenance"
        score = await evaluator.evaluate(
            subject=diff_text,
            context=f"Mission: {mission_ctx}\nAgent cycle {cycle + 1} code changes.",
            mode="implement",
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
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

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
# Meta-review
# ---------------------------------------------------------------------------

async def run_meta_review(
    llm: LLM,
    cycle: int,
    score_history: list[dict[str, Any]],
    since_hash: str,
    current_notes: str,
    repo_path: Path,
    write_fn: Callable[..., Any],
) -> MetaReviewResult:
    """Run the periodic meta-review LLM call.

    Returns a ``MetaReviewResult`` with the strategic direction text and
    the current HEAD hash.  On failure, returns empty context so the
    caller can safely proceed.
    """
    log.info("agent_eval: running meta-review after cycle %d", cycle + 1)

    # Build the user message from template
    score_table = format_score_history(score_history)
    git_delta = await agent_git.get_review_git_delta(
        repo_path, since_hash or "HEAD~20",
    )
    notes = current_notes
    if len(notes) > 3000:
        log.debug("agent_eval: meta-review notes truncated from %d to 3k chars", len(notes))
        notes = notes[-3000:]

    user_content = meta_review_prompts.AGENT_META_REVIEW_USER
    user_content = user_content.replace("$score_history", score_table)
    user_content = user_content.replace("$git_delta", git_delta)
    user_content = user_content.replace("$current_notes", notes)

    try:
        response = await llm.call(
            [{"role": "user", "content": user_content}],
            system=meta_review_prompts.AGENT_META_REVIEW_SYSTEM,
        )
        context = (response.text or "").strip()
        head_hash = await agent_git.get_head_hash(repo_path)
        log.info(
            "agent_eval: meta-review complete (%d chars), hash=%s",
            len(context), head_hash,
        )
        # Persist the review to artifacts
        try:
            write_fn(context, f"cycle_{cycle + 1}", "meta_review.md")
        except Exception:
            pass
        return MetaReviewResult(context=context, head_hash=head_hash)
    except Exception as exc:
        log.warning("agent_eval: meta-review failed: %s", exc)
        return MetaReviewResult(context="", head_hash=since_hash)
