"""DualEvaluator — two independent evaluators that never see each other's output.

Unlike ThreeWayResolver (which merges perspectives), this keeps evaluators
isolated to prevent groupthink.  Scores are combined numerically.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from harness.llm import LLM
from harness.phase import DualScore, ScoreItem
from harness.prompts import dual_evaluator as default_prompts

log = logging.getLogger(__name__)


def parse_score(text: str, pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)") -> float:
    """Extract a numeric score from evaluator output.  Returns 0.0 if not found."""
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


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

        # Run in parallel — key: neither sees the other's output
        basic_task = self.llm.call(list(messages), system=basic_sys)
        diffusion_task = self.llm.call(list(messages), system=diffusion_sys)

        basic_resp, diffusion_resp = await asyncio.gather(basic_task, diffusion_task)

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
