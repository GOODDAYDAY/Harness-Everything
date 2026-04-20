"""edit_file — search/replace within a file."""

from __future__ import annotations

import asyncio
import os
import tempfile
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
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_path, path_validated = await self._validate_atomic_path(config, path)
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        resolved = path_validated
        if scope_err := self._check_phase_scope(config, resolved):
            return scope_err

        # Use the shared atomic read helper
        text, read_error = await self._atomic_read_text(config, resolved)
        if read_error is not None:
            return read_error
        count = text.count(old_str)

        if count == 0:
            return ToolResult(error="old_str not found in file", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                error=f"old_str appears {count} times — set replace_all=true or provide more context",
                is_error=True,
            )

        new_text = text.replace(old_str, new_str, -1 if replace_all else 1)
        
        # Write back using the new async atomic helper
        write_error = await self._atomic_write_text(resolved, new_text)
        if write_error is not None:
            return write_error
        
        replaced = count if replace_all else 1
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
