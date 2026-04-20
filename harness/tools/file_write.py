"""write_file — create or overwrite a file."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation


@enforce_atomic_validation
class WriteFileTool(Tool):
    name = "write_file"
    description = "Create a new file or completely overwrite an existing file with the given content."
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(
        self, config: HarnessConfig, *, path: str, content: str
    ) -> ToolResult:
        # First, validate the path is within allowed directories
        # For write operations, we don't require the file or parent directories to exist
        path_result = self._check_path(config, path, require_exists=False)
        if isinstance(path_result, ToolResult):
            return path_result  # This is a ToolResult error
        resolved = path_result

        # Validate parent directory atomically to prevent TOCTOU symlink attacks
        parent_dir = Path(resolved).parent
        if str(parent_dir) != ".":  # Skip if parent is current directory
            is_valid_parent, parent_result = await self._validate_and_prepare_parent_directory(
                config, str(parent_dir), require_exists=False, check_scope=True
            )
            if not is_valid_parent:
                return parent_result  # This is a ToolResult error
            # Ensure parent directories exist
            await asyncio.to_thread(parent_dir.mkdir, parents=True, exist_ok=True)

        # Write back using the async atomic helper
        write_error = await self._atomic_write_text(resolved, content)
        if write_error is not None:
            return write_error
        
        return ToolResult(output=f"Wrote {len(content)} bytes to {resolved}")

    async def _atomic_read_text(self, config, path):
        """Read file content atomically with TOCTOU protection.
        
        This is a wrapper around the base class implementation for consistency
        with other file operation tools.
        """
        return await super()._atomic_read_text(config, path)
