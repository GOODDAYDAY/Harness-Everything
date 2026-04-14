"""ThreeWayResolver — the shared conservative/aggressive/merge pattern."""

from __future__ import annotations

import asyncio
import logging
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
        conservative_task = self.llm.call(messages, system=conservative_system)
        aggressive_task = self.llm.call(messages, system=aggressive_system)

        conservative_resp, aggressive_resp = await asyncio.gather(
            conservative_task, aggressive_task
        )

        log.info("Three-way: conservative=%d chars, aggressive=%d chars",
                 len(conservative_resp.text), len(aggressive_resp.text))

        # Phase 2: merge
        merge_messages = [
            {
                "role": "user",
                "content": (
                    f"## Conservative Proposal\n\n{conservative_resp.text}\n\n"
                    f"## Aggressive Proposal\n\n{aggressive_resp.text}\n\n"
                    f"## Original Request\n\n{user_message}\n\n"
                    "Please merge these two proposals into a single coherent plan."
                ),
            }
        ]

        merge_resp = await self.llm.call(merge_messages, system=merge_system)

        return ThreeWayResult(
            conservative=conservative_resp.text,
            aggressive=aggressive_resp.text,
            merged=merge_resp.text,
        )
