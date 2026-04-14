"""Planner — three-way planning with conservative/aggressive/merge."""

from __future__ import annotations

import logging

from harness.config import HarnessConfig
from harness.llm import LLM
from harness.three_way import ThreeWayResolver
from harness.prompts import planner as default_prompts

log = logging.getLogger(__name__)


class Planner:
    """Generate an implementation plan via three-way resolution."""

    def __init__(self, llm: LLM, config: HarnessConfig) -> None:
        self.llm = llm
        self.config = config
        self.resolver = ThreeWayResolver(llm)

    async def plan(self, task: str, context: str = "") -> str:
        """Return a merged implementation plan.

        Args:
            task: The user's task description.
            context: Optional context — file contents, prior feedback, etc.
        """
        user_message = f"## Task\n\n{task}"
        if context:
            user_message += f"\n\n## Context\n\n{context}"

        cfg = self.config.planner

        result = await self.resolver.resolve(
            user_message,
            conservative_system=cfg.conservative_system or default_prompts.CONSERVATIVE_SYSTEM,
            aggressive_system=cfg.aggressive_system or default_prompts.AGGRESSIVE_SYSTEM,
            merge_system=cfg.merge_system or default_prompts.MERGE_SYSTEM,
        )

        log.info(
            "Planner: conservative=%d chars, aggressive=%d chars, merged=%d chars",
            len(result.conservative), len(result.aggressive), len(result.merged),
        )

        return result.merged
