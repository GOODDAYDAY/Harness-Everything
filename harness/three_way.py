"""ThreeWayResolver — the shared conservative/aggressive/merge pattern."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from harness.llm import LLM

log = logging.getLogger(__name__)

# Minimum word counts for each phase.  Below these thresholds the output is
# almost certainly a refusal, a placeholder, or a truncation artefact; the
# merge step would produce garbage because it has nothing substantive to work
# with.  Values are deliberately low to avoid spurious retries on genuinely
# brief but correct responses (e.g. "1. FILE: x.py\n   CHANGE: …").
_MIN_PERSPECTIVE_WORDS: int = 60   # conservative / aggressive proposals
_MIN_MERGE_WORDS: int = 40         # merged plan / verdict

# Prefix injected into the retry message so the model understands why it is
# being asked again — without this, it often produces the same short output.
_RETRY_PREAMBLE = (
    "[RETRY — your previous response was too short to be useful "
    "({words} words). Please produce a complete, detailed response "
    "following all instructions in your system prompt.]\n\n"
)


def _is_response_too_short(text: str, min_words: int) -> bool:
    """Return True when *text* contains fewer than *min_words* words."""
    return len(text.split()) < min_words


async def _call_with_short_output_retry(
    llm: LLM,
    messages: list[dict],
    *,
    system: str,
    min_words: int,
    label: str,
) -> Any:
    """Call the LLM and retry once if the response is suspiciously short.

    On retry the original message is prepended with a preamble that tells the
    model its previous response was inadequate, which breaks the pattern of
    the model reasoning itself into a short "I can't do this" response.

    Args:
        llm:       The LLM instance to call.
        messages:  The conversation messages list.
        system:    System prompt for the call.
        min_words: Minimum acceptable word count in the response.
        label:     Human-readable label for log messages (e.g. "conservative").

    Returns:
        The LLMResponse from the first acceptable call (or the retry if the
        first was too short).
    """
    resp = await llm.call(messages, system=system)

    if not _is_response_too_short(resp.text, min_words):
        return resp

    words = len(resp.text.split())
    log.warning(
        "three_way %s: response too short (%d words < %d min) — retrying",
        label, words, min_words,
    )

    # Build a retry message that preserves the original context and adds an
    # explicit nudge.  We append as a new user turn so the model sees its own
    # short response as the prior assistant turn, making the retry more natural.
    preamble = _RETRY_PREAMBLE.format(words=words)
    retry_messages: list[dict] = list(messages)
    if resp.text.strip():
        # Include the short response as an assistant turn so the model knows
        # exactly what we found insufficient.
        retry_messages = retry_messages + [
            {"role": "assistant", "content": resp.text},
            {"role": "user", "content": preamble},
        ]
    else:
        # If the response was completely empty, just re-send with the preamble
        # prepended to the original user message.
        first_msg = retry_messages[0]
        retry_messages = [
            {
                "role": first_msg["role"],
                "content": preamble + first_msg["content"],
            }
        ] + retry_messages[1:]

    retry_resp = await llm.call(retry_messages, system=system)
    retry_words = len(retry_resp.text.split())

    if _is_response_too_short(retry_resp.text, min_words):
        log.warning(
            "three_way %s: retry also short (%d words) — proceeding with best effort",
            label, retry_words,
        )
        # Return whichever response was longer — don't silently discard the
        # retry result even if it is still short.
        return retry_resp if retry_words >= words else resp

    log.info(
        "three_way %s: retry produced %d words (was %d) — using retry",
        label, retry_words, words,
    )
    return retry_resp


@dataclass
class ThreeWayResult:
    """Outputs from one three-way resolution pass."""

    conservative: str
    aggressive: str
    merged: str


class ThreeWayResolver:
    """Run two parallel perspectives then merge them into one.

    Used by both Planner and Evaluator.  No tool_use — pure reasoning.

    Short-output guard
    ------------------
    Each of the three LLM calls is guarded against suspiciously short outputs
    (< ``_MIN_PERSPECTIVE_WORDS`` / ``_MIN_MERGE_WORDS`` words).  When a
    response falls below the threshold, a single retry is attempted with an
    explicit nudge that tells the model its previous response was too brief.
    This prevents silent garbage-in to garbage-out chains where a one-line
    conservative response causes the merger to produce a one-line plan.

    The guard is intentionally lenient (60 words for perspectives, 40 for
    merge) to avoid spurious retries on genuinely brief but correct outputs.
    """

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    async def resolve(
        self,
        user_message: str,
        *,
        conservative_system: str,
        aggressive_system: str,
        merge_system: str,
    ) -> ThreeWayResult:
        """Run the three-way pattern and return all three outputs."""

        messages = [{"role": "user", "content": user_message}]

        # Phase 1: conservative + aggressive in parallel, each with short-output guard.
        # Wrap coroutines in Tasks so that if one raises we can explicitly cancel
        # the other rather than leaving an abandoned background task consuming
        # API quota and emitting "Task exception was never retrieved" warnings.
        t0 = time.monotonic()
        conservative_task = asyncio.ensure_future(_call_with_short_output_retry(
            self.llm, messages,
            system=conservative_system,
            min_words=_MIN_PERSPECTIVE_WORDS,
            label="conservative",
        ))
        aggressive_task = asyncio.ensure_future(_call_with_short_output_retry(
            self.llm, messages,
            system=aggressive_system,
            min_words=_MIN_PERSPECTIVE_WORDS,
            label="aggressive",
        ))

        try:
            conservative_resp, aggressive_resp = await asyncio.gather(
                conservative_task, aggressive_task
            )
        except Exception:
            for t in (conservative_task, aggressive_task):
                if not t.done():
                    t.cancel()
            raise
        phase1_elapsed = time.monotonic() - t0

        cons_words = len(conservative_resp.text.split())
        aggr_words = len(aggressive_resp.text.split())
        log.info(
            "Three-way phase1: conservative=%d chars (%d words), "
            "aggressive=%d chars (%d words)  (%.1fs parallel)",
            len(conservative_resp.text), cons_words,
            len(aggressive_resp.text), aggr_words,
            phase1_elapsed,
        )

        # Phase 2: merge.
        # We include a brief meta-note with word counts so the merger can
        # gauge which proposal is more detailed, and a reminder of the original
        # request at the end so it stays in the recency window of the context.
        merge_messages = [
            {
                "role": "user",
                "content": (
                    f"## Conservative Proposal  ({cons_words} words)\n\n"
                    f"{conservative_resp.text}\n\n"
                    f"## Aggressive Proposal  ({aggr_words} words)\n\n"
                    f"{aggressive_resp.text}\n\n"
                    f"## Original Request (repeated for reference)\n\n"
                    f"{user_message}\n\n"
                    "Merge the two proposals above following your system instructions. "
                    "Explicitly state which proposal each step was drawn from and why."
                ),
            }
        ]

        t1 = time.monotonic()
        merge_resp = await _call_with_short_output_retry(
            self.llm, merge_messages,
            system=merge_system,
            min_words=_MIN_MERGE_WORDS,
            label="merge",
        )
        log.info(
            "Three-way phase2 (merge): %d chars (%d words)  (%.1fs)",
            len(merge_resp.text), len(merge_resp.text.split()),
            time.monotonic() - t1,
        )

        return ThreeWayResult(
            conservative=conservative_resp.text,
            aggressive=aggressive_resp.text,
            merged=merge_resp.text,
        )
