"""DualEvaluator — two independent evaluators that never see each other's output.

Unlike ThreeWayResolver (which merges perspectives), this keeps evaluators
isolated to prevent groupthink.  Scores are combined numerically.
"""

from __future__ import annotations

import asyncio
import logging
import re

from harness.core.llm import LLM
from harness.pipeline.phase import DualScore, ScoreItem
from harness.prompts import dual_evaluator as default_prompts

log = logging.getLogger(__name__)


_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0


def parse_score(text: str, pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)") -> float:
    """Extract a numeric score from evaluator output and clamp it to [0, 10].

    Returns 0.0 when the pattern is not found.  Logs a warning when the raw
    value is outside the expected range so misconfigured score patterns and
    hallucinated values are visible in the run log.

    Uses ``re.findall`` and takes the **last** match rather than ``re.search``
    (which returns the first).  Evaluator prompts instruct the LLM to show
    their arithmetic inline (e.g. ``SCORE = (A×0.4)+… = 6.0``) *before*
    writing the authoritative ``SCORE: 6.4`` at the end.  The first
    ``SCORE:``-prefixed number is therefore often an intermediate value from
    the weighted arithmetic, not the final verdict.  Taking the last match
    reliably selects the declared final score regardless of how many
    arithmetic lines precede it.
    """
    matches = re.findall(pattern, text, re.IGNORECASE)
    if not matches:
        return 0.0
    raw = float(matches[-1])
    clamped = max(_SCORE_MIN, min(_SCORE_MAX, raw))
    if clamped != raw:
        log.warning(
            "parse_score: raw value %.2f is outside [%.0f, %.0f] — clamped to %.2f",
            raw, _SCORE_MIN, _SCORE_MAX, clamped,
        )
    return clamped


class DualEvaluator:
    """Run two evaluators in parallel, each blind to the other's output."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    async def evaluate(
        self,
        subject: str,
        context: str,
        *,
        basic_system: str = "",
        diffusion_system: str = "",
        score_pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
    ) -> DualScore:
        """Run both evaluators in parallel and return combined scores.

        Args:
            subject: What to evaluate (proposal text, code state, etc.).
            context: Source files, architecture constraints, etc.
            basic_system: System prompt for the adversarial evaluator.
            diffusion_system: System prompt for the second-order effects evaluator.
            score_pattern: Regex to extract numeric score from evaluator output.
        """
        basic_sys = basic_system or default_prompts.BASIC_SYSTEM
        diffusion_sys = diffusion_system or default_prompts.DIFFUSION_SYSTEM

        # Build user messages (identical structure, different system prompts)
        messages = [
            {
                "role": "user",
                "content": (
                    f"## Subject to Evaluate\n\n{subject}\n\n"
                    f"## Source Context\n\n{context}"
                ),
            }
        ]

        # Run in parallel — key: neither sees the other's output.
        # Wrap coroutines in Tasks so that if one raises, we can explicitly
        # cancel the other rather than leaving it as an abandoned background
        # task that continues consuming API quota and logs an unhandled
        # "Task exception was never retrieved" warning.
        basic_task = asyncio.ensure_future(self.llm.call(list(messages), system=basic_sys))
        diffusion_task = asyncio.ensure_future(self.llm.call(list(messages), system=diffusion_sys))

        try:
            basic_resp, diffusion_resp = await asyncio.gather(basic_task, diffusion_task)
        except Exception:
            # Cancel whichever task is still running so it does not linger
            # as a background coroutine consuming API quota.
            for t in (basic_task, diffusion_task):
                if not t.done():
                    t.cancel()
            raise

        basic_score = parse_score(basic_resp.text, score_pattern)
        diffusion_score = parse_score(diffusion_resp.text, score_pattern)

        log.info(
            "DualEvaluator: basic=%.1f diffusion=%.1f combined=%.1f",
            basic_score, diffusion_score, basic_score + diffusion_score,
        )

        return DualScore(
            basic=ScoreItem(basic_score, basic_resp.text),
            diffusion=ScoreItem(diffusion_score, diffusion_resp.text),
        )
