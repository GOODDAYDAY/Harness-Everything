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
        
        # Explicit validation for empty-string-to-empty-string replacement
        if old_str == "" and new_str == "" and not replace_all:
            return ToolResult(
                error="Replacing empty string with empty string is a no-op and requires replace_all=True to confirm intent",
                is_error=True
            )
        
        # Special handling for empty string replacement
        if old_str == "":
            # Empty string replacement is a special case
            # For empty files, we allow replacement (treat as count = 1)
            # For non-empty files, we require replace_all=True due to ambiguity
            if text == "":
                # Empty file: allow replacement
                count = 1
            else:
                # Non-empty file: count is ambiguous with str.count("")
                # It returns len(text) + 1, which doesn't match user intuition
                if not replace_all:
                    return ToolResult(
                        error="Empty string replacement in non-empty files requires replace_all=True due to ambiguity",
                        is_error=True
                    )
                # When new_str is also empty, this is a no-op
                if new_str == "":
                    count = 0
                else:
                    count = len(text) + 1
        else:
            count = text.count(old_str)

        if count == 0:
            # Special case: empty-to-empty replacement with replace_all=True is a no-op
            if old_str == "" and new_str == "" and replace_all:
                # This is a valid no-op operation
                pass
            else:
                return ToolResult(error="old_str not found in file", is_error=True)
        if count > 1 and not replace_all:
            # Find line numbers where old_str appears for better error messages
            lines = text.splitlines(keepends=True)
            line_numbers = []
            current_pos = 0
            for i, line in enumerate(lines, 1):
                if old_str in line:
                    line_numbers.append(i)
                current_pos += len(line)
            
            line_info = f" on lines {', '.join(map(str, line_numbers[:5]))}"
            if len(line_numbers) > 5:
                line_info += f" and {len(line_numbers) - 5} more"
            
            return ToolResult(
                error=f"old_str appears {count} times{line_info} — set replace_all=true or provide more context",
                is_error=True,
            )

        new_text = text.replace(old_str, new_str, -1 if replace_all else 1)
        
        # Use consolidated atomic validation and write
        write_result = await self._atomic_validate_and_write(
            config, path, new_text, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        if write_result.is_error:
            return write_result
        
        # Calculate actual number of replacements made
        if old_str == "":
            if text == "":
                replaced = 1 if new_str != "" else 0
            else:
                replaced = len(text) + 1 if new_str != "" else 0
        else:
            replaced = count if replace_all else 1
        
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
