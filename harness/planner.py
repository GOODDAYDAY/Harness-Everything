"""Planner — three-way planning with conservative/aggressive/merge."""

from __future__ import annotations

import logging
import re

from harness.core.config import HarnessConfig
from harness.core.llm import LLM
from harness.three_way import ThreeWayResolver
from harness.prompts import planner as default_prompts

log = logging.getLogger(__name__)

# The merge prompt instructs the LLM to append a ``RISKS:`` section after the
# numbered steps.  That section is useful for human review but should NOT be
# handed to the executor — it has no tool-use semantics and may be
# mis-interpreted as plan steps (e.g. "- risk 1: …" looks like a bullet step).
# This regex matches the RISKS trailer: "RISKS:\n- …\n- …" up to end-of-string.
# We strip it from the plan before returning so the executor only sees numbered
# implementation steps.
_RISKS_SECTION_RE = re.compile(
    r"\n*^RISKS:\s*\n(?:^[-*•]\s+.+\n?)*",
    re.MULTILINE,
)


def _strip_risks_section(plan: str) -> str:
    """Remove the optional ``RISKS:`` trailer from a merged plan.

    The RISKS block is written by the merge-system prompt for human readers.
    The executor does not understand it and may try to "execute" risk bullets
    as if they were plan steps, wasting tool turns.

    Logs at DEBUG when a RISKS section is found and stripped so that the
    removal is visible in verbose runs without polluting normal INFO logs.
    """
    stripped, n = _RISKS_SECTION_RE.subn("", plan)
    if n:
        log.debug(
            "Planner: stripped RISKS section from merged plan (%d → %d chars)",
            len(plan),
            len(stripped),
        )
        return stripped.rstrip()
    return plan


class Planner:
    """Generate an implementation plan via three-way resolution."""

    def __init__(self, llm: LLM, config: HarnessConfig) -> None:
        self.llm = llm
        self.config = config
        self.resolver = ThreeWayResolver(llm)

    async def plan(self, task: str, context: str = "") -> str:
        """Return a merged implementation plan ready for the executor.

        Steps:
        1. Build a user message combining the task description and optional
           context (file contents, prior feedback, etc.).
        2. Run three-way resolution (conservative + aggressive + merge).
        3. Strip the ``RISKS:`` trailer that the merge prompt may append —
           it is informational for humans but must not reach the executor.
        4. Return the cleaned plan string.

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

        plan = _strip_risks_section(result.merged)

        log.info(
            "Planner: conservative=%d chars, aggressive=%d chars, "
            "merged=%d chars (plan=%d chars after RISKS strip)",
            len(result.conservative),
            len(result.aggressive),
            len(result.merged),
            len(plan),
        )

        return plan
