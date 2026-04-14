"""ThreeWayResolver — the shared conservative/aggressive/merge pattern."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from harness.llm import LLM

log = logging.getLogger(__name__)


@dataclass
class ThreeWayResult:
    conservative: str
    aggressive: str
    merged: str


class ThreeWayResolver:
    """Run two parallel perspectives then merge them into one.

    Used by both Planner and Evaluator.  No tool_use — pure reasoning.
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

        # Phase 1: conservative + aggressive in parallel
        t0 = time.monotonic()
        conservative_task = self.llm.call(messages, system=conservative_system)
        aggressive_task = self.llm.call(messages, system=aggressive_system)

        conservative_resp, aggressive_resp = await asyncio.gather(
            conservative_task, aggressive_task
        )
        phase1_elapsed = time.monotonic() - t0

        cons_words = len(conservative_resp.text.split())
        aggr_words = len(aggressive_resp.text.split())
        log.info(
            "Three-way phase1: conservative=%d chars (%d words), aggressive=%d chars (%d words)  (%.1fs parallel)",
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
        merge_resp = await self.llm.call(merge_messages, system=merge_system)
        log.info(
            "Three-way phase2 (merge): %d chars  (%.1fs)",
            len(merge_resp.text), time.monotonic() - t1,
        )

        return ThreeWayResult(
            conservative=conservative_resp.text,
            aggressive=aggressive_resp.text,
            merged=merge_resp.text,
        )
