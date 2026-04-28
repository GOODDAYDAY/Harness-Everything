"""UpdateSkillTool — create or update skill files at runtime."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class UpdateSkillTool(Tool):
    """Create or update a project skill (SKILL.md) at runtime.

    Skills are persistent knowledge documents stored in
    ``<workspace>/.harness/skills/<name>/SKILL.md``.  Use this tool to
    capture recurring project knowledge that should survive across
    cycles: architecture patterns, diagnostic SQL, workflow guides,
    domain constraints.
    """

    name = "skill_update"
    description = (
        "Create or update a project skill file. "
        "name: skill identifier (lowercase, hyphens ok). "
        "description: one-line summary. "
        "body: markdown content. "
        "auto_load: if true, injected into system prompt every cycle "
        "(default false — use sparingly, shares a 12K char budget)."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    _VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Skill name (lowercase alphanumeric, hyphens, underscores). "
                        "Used as the directory name under .harness/skills/."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this skill covers.",
                },
                "body": {
                    "type": "string",
                    "description": "Markdown body — the knowledge content of the skill.",
                },
                "auto_load": {
                    "type": "boolean",
                    "description": (
                        "If true, skill body is injected into system prompt every "
                        "cycle. Use sparingly — auto-loaded skills share a 12K "
                        "char budget. Default: false."
                    ),
                    "default": False,
                },
            },
            "required": ["name", "description", "body"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        name: str,
        description: str,
        body: str,
        auto_load: bool = False,
    ) -> ToolResult:
        # Validate name.
        if not self._VALID_NAME.match(name):
            return ToolResult(
                error=(
                    "Skill name must be lowercase alphanumeric with hyphens or "
                    "underscores, starting with a letter or digit. "
                    f"Got: {name!r}"
                ),
                is_error=True,
            )
        if name.startswith("_"):
            return ToolResult(
                error="Skill names starting with '_' are reserved for virtual skills.",
                is_error=True,
            )

        # Build target path.
        skills_dir = Path(config.workspace) / ".harness" / "skills" / name
        skill_path = skills_dir / "SKILL.md"

        # Security: ensure path is within workspace.
        resolved = str(skill_path.resolve())
        ws_resolved = str(Path(config.workspace).resolve())
        if not resolved.startswith(ws_resolved):
            return ToolResult(
                error=f"PERMISSION ERROR: skill path is outside workspace",
                is_error=True,
            )

        # Build SKILL.md content.
        auto_str = "true" if auto_load else "false"
        # Escape description for frontmatter (wrap in quotes if it contains colons).
        desc_fm = f'"{description}"' if ":" in description else description
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {desc_fm}\n"
            f"auto_load: {auto_str}\n"
            f"---\n\n"
            f"{body.strip()}\n"
        )

        # Write file.
        try:
            skills_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                error=f"Failed to write skill: {exc}",
                is_error=True,
            )

        # Update live registry if attached.
        registry = getattr(config, "skill_registry", None)
        if registry is not None:
            try:
                from harness.skills.loader import load_skill

                skill = load_skill(skill_path)
                registry.register(skill)
            except Exception:
                pass  # file written; registry update is best-effort

        return ToolResult(
            output=f"Skill '{name}' written to {skill_path} ({len(content)} chars)",
            metadata={"skill_name": name, "path": str(skill_path)},
        )
