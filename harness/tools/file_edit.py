"""edit_file — search/replace within a file."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation


@enforce_atomic_validation
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
        # Use consolidated atomic validation and read
        read_result = await self._atomic_validate_and_read(
            config, path, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        if isinstance(read_result, ToolResult):
            return read_result  # Error from validation or read
        text, resolved = read_result
        
        count = text.count(old_str)

        if count == 0:
            return ToolResult(error="old_str not found in file", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                error=f"old_str appears {count} times — set replace_all=true or provide more context",
                is_error=True,
            )

        new_text = text.replace(old_str, new_str, -1 if replace_all else 1)
        
        # Use consolidated atomic validation and write
        write_result = await self._atomic_validate_and_write(
            config, path, new_text, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        if write_result.is_error:
            return write_result
        
        replaced = count if replace_all else 1
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
