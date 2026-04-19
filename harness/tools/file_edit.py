"""edit_file — search/replace within a file."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

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

    def _guaranteed_fd_cleanup(self, fd: int, operation: Callable[[int], Any]) -> Tuple[Any, Optional[ToolResult]]:
        """
        Execute `operation(fd)` and guarantee `os.close(fd)` is called on failure.
        Returns (result, None) on success, or (None, ToolResult) on failure.
        
        On success, ownership of `fd` is transferred to the result of `operation`.
        On failure, `fd` is closed before returning an error.
        """
        try:
            result = operation(fd)  # e.g., os.fdopen(fd, 'rb')
            return result, None
        except Exception as exc:
            # Close fd only on operation failure
            try:
                os.close(fd)
            except OSError:
                pass  # FD may already be closed; ignore secondary error
            return None, ToolResult(error=f"File operation failed on descriptor {fd}: {exc}", is_error=True)

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

        # Use atomic file opening to prevent TOCTOU attacks
        fd, error = await asyncio.to_thread(self._open_with_atomic_fallback, resolved, os.O_RDONLY)
        if error is not None:
            return ToolResult(error=f"Cannot open file for editing: {error.error}", is_error=True)
        
        # Use the helper to safely convert fd to a file object
        def fdopen_operation(fd: int):
            return os.fdopen(fd, 'rb')
        
        file_obj, open_error = await asyncio.to_thread(self._guaranteed_fd_cleanup, fd, fdopen_operation)
        if open_error is not None:
            return open_error
        # file_obj is now guaranteed to be open, and the original fd is closed.
        
        try:
            # Read binary and decode with same error handling as ReadFileTool
            content = file_obj.read()
            text = content.decode('utf-8', errors='replace')
        except Exception as exc:
            return ToolResult(error=f"Failed to read file: {exc}", is_error=True)
        finally:
            file_obj.close()
        count = text.count(old_str)

        if count == 0:
            return ToolResult(error="old_str not found in file", is_error=True)
        if count > 1 and not replace_all:
            return ToolResult(
                error=f"old_str appears {count} times — set replace_all=true or provide more context",
                is_error=True,
            )

        new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        
        # Write back using atomic write pattern (temp file + os.replace)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(resolved), delete=False
            ) as tmp:
                tmp.write(new_text)
                tmp_path = tmp.name
            os.replace(tmp_path, resolved)
        except Exception as exc:
            # Clean up temp file if it exists
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            return ToolResult(error=f"Failed to write file: {exc}", is_error=True)
        
        replaced = count if replace_all else 1
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
