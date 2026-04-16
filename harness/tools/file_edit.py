"""edit_file — search/replace within a file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Perform an exact string replacement in a file. "
        "old_str must appear exactly once in the file (unless replace_all is true)."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_str": {"type": "string", "description": "Exact text to find"},
                "new_str": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false)",
                    "default": False,
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> ToolResult:
        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err

        p = Path(resolved)
        if not p.is_file():
            return ToolResult(error=f"File not found: {resolved}", is_error=True)

        text = p.read_text(encoding="utf-8")
        count = text.count(old_str)

        if count == 0:
            return ToolResult(error="old_str not found in file", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                error=f"old_str appears {count} times — set replace_all=true or provide more context",
                is_error=True,
            )

        new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        p.write_text(new_text, encoding="utf-8")
        replaced = count if replace_all else 1
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
