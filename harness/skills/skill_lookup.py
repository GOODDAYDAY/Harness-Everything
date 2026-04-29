"""SkillLookupTool — read-only tool for loading skill content at runtime."""

from __future__ import annotations

import logging
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)


class SkillLookupTool(Tool):
    """Load the full content of a project skill by name.

    Skills provide project-specific knowledge: architecture docs, SQL
    diagnostic patterns, workflow guides, domain constraints.  The
    system prompt lists available skills in a compact index; use this
    tool to load the complete body of any listed skill.

    Called with no arguments: returns the compact index of all skills.
    Called with a name: returns the full SKILL.md body for that skill.
    """

    name = "skill_lookup"
    description = (
        "Load a project skill by name to get deeper context. "
        "No args: list all available skills. "
        "name='architecture': load the full content of that skill. "
        "Skills contain project-specific knowledge that supplements "
        "the auto-loaded context in your system prompt."
    )
    requires_path_check = False
    tags = frozenset({"analysis"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Exact skill name to load (e.g. 'sql-diagnostics'). "
                        "Omit to list all available skills."
                    ),
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        name: str = "",
    ) -> ToolResult:
        from harness.skills.registry import SkillRegistry

        registry: SkillRegistry | None = getattr(config, "skill_registry", None)
        if registry is None:
            return ToolResult(
                error=(
                    "No skill registry available. "
                    "Skills are not configured for this project."
                ),
                is_error=True,
            )

        # No name → return compact index of all skills.
        if not name:
            index = registry.compact_index()
            if not index:
                return ToolResult(output="No on-demand skills available.")
            log.info("skills: skill_lookup — listing index (%d skills)", len(registry.names))
            return ToolResult(output=index)

        # Look up the requested skill.
        log.info("skills: skill_lookup — loading skill %r", name)
        skill = registry.get(name)
        if skill is None:
            available = sorted(registry.names)
            return ToolResult(
                error=(
                    f"Skill {name!r} not found. "
                    f"Available: {available}"
                ),
                is_error=True,
            )

        if not skill.body:
            return ToolResult(
                output=(
                    f"Skill {name!r} has no body content "
                    f"(description: {skill.description})"
                ),
            )

        # For disk-backed skills, re-read from disk (hot-reload).
        if skill.path and not skill.is_virtual:
            try:
                from harness.skills.loader import load_skill

                fresh = load_skill(skill.path)
                return ToolResult(
                    output=f"## Skill: {fresh.name}\n\n{fresh.body}",
                    metadata={
                        "skill_name": fresh.name,
                        "char_count": fresh.char_count,
                    },
                )
            except Exception:
                pass  # fall through to cached version

        return ToolResult(
            output=f"## Skill: {skill.name}\n\n{skill.body}",
            metadata={
                "skill_name": skill.name,
                "char_count": skill.char_count,
            },
        )
