"""DualEvaluator — two independent evaluators that never see each other's output.

Unlike ThreeWayResolver (which merges perspectives), this keeps evaluators
isolated to prevent groupthink.  Scores are combined numerically.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from harness.core.llm import LLM
from harness.pipeline.phase import DualScore, ScoreItem
from harness.prompts import evaluator as default_prompts

log = logging.getLogger(__name__)


_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0

# Strict pattern: "SCORE: N" on its own line (anchored).  Preferred over loose
# because evaluators are instructed to place the authoritative score last on
# its own line.  The loose fallback handles older/custom prompts that don't
# follow the anchored format.
_STRICT_RE = re.compile(r"^SCORE:\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)
_LOOSE_RE  = re.compile(r"SCORE[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)

# Mode header injected into the evaluation user message so evaluators know
# whether they are reviewing a text proposal or an implement-mode code change.
_MODE_HEADERS: dict[str, str] = {
    "debate": (
        "## Evaluation Mode: DEBATE\n"
        "You are reviewing a **text proposal** (plan / recommendation).\n"
        "Evaluate the plan's specificity, completeness, and correctness of reasoning.\n"
        "Do NOT penalise for lack of executed tool calls — this is a planning round.\n\n"
    ),
    "implement": (
        "## Evaluation Mode: IMPLEMENT\n"
        "You are reviewing an **executed code change** (implement round).\n"
        "Evaluate the actual code state after execution: correctness of edits, "
        "test results, and tool call success/failure, not the quality of the plan.\n"
        "A proposal section may be present for context but the CODE STATE is "
        "the authoritative subject.\n\n"
    ),
}


def parse_score(
    text: str,
    pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
) -> float:
    """Extract a numeric score from evaluator output and clamp it to [0, 10].

    Extraction strategy (two-tier):
    1. **Strict** — search for ``^SCORE: N$`` (anchored to line boundaries).
       Takes the **last** strict match.  This reliably captures the
       authoritative final score placed at the end of the output, ignoring
       any inline arithmetic lines such as ``SCORE = (A×0.4)+… = 6.0``.
    2. **Loose fallback** — if no strict match, apply the caller-supplied
       ``pattern`` (default: ``SCORE[:\\s]+N``).  Takes the last match.

    Returns 0.0 and logs a warning when no match is found.
    Logs a warning when the extracted value is outside [0, 10].
    """
    strict = _STRICT_RE.findall(text)
    if strict:
        raw = float(strict[-1])
    else:
        loose = re.findall(pattern, text, re.IGNORECASE)
        if not loose:
            log.warning(
                "parse_score: no score token found in evaluator output (len=%d)",
                len(text),
            )
            return 0.0
        raw = float(loose[-1])

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
        mode: Literal["debate", "implement"] = "debate",
        basic_system: str = "",
        diffusion_system: str = "",
        score_pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
    ) -> DualScore:
        """Run both evaluators in parallel and return combined scores.

        Args:
            subject: What to evaluate (proposal text, code state, etc.).
            context: Source files, architecture constraints, etc.
            mode: ``"debate"`` for text proposals, ``"implement"`` for executed
                code changes.  Selects the appropriate default system prompts
                and prepends a mode header to the evaluation user message so
                evaluators apply the correct rubric.
            basic_system: Override system prompt for the basic evaluator.
            diffusion_system: Override system prompt for the diffusion evaluator.
            score_pattern: Regex to extract numeric score from evaluator output
                (used as the loose fallback in parse_score).
        """
        basic_sys = basic_system or default_prompts.BASIC_SYSTEM
        diffusion_sys = diffusion_system or default_prompts.DIFFUSION_SYSTEM

        # Prepend a mode header so evaluators adapt their rubric to whether
        # they are reviewing a text proposal or an executed code change.
        mode_header = _MODE_HEADERS.get(mode, _MODE_HEADERS["debate"])

        # Build user messages (identical structure, different system prompts)
        messages = [
            {
                "role": "user",
                "content": (
                    f"{mode_header}"
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
            "DualEvaluator[%s]: basic=%.1f diffusion=%.1f combined=%.1f",
            mode, basic_score, diffusion_score, basic_score + diffusion_score,
        )

        return DualScore(
            basic=ScoreItem(basic_score, basic_resp.text),
            diffusion=ScoreItem(diffusion_score, diffusion_resp.text),
        )
