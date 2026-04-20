"""write_file — create or overwrite a file."""

from __future__ import annotations

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
        # Use atomic validation for the file path to prevent TOCTOU attacks
        # For write operations, we don't require the file to exist
        is_valid_path, path_validated = await self._validate_atomic_path(config, path, require_exists=False, check_scope=True)
        if not is_valid_path:
            return path_validated  # This is a ToolResult error
        resolved = path_validated

        # Write back using the async atomic helper
        write_error = await self._atomic_write_text(resolved, content)
        if write_error is not None:
            return write_error
        
        return ToolResult(output=f"Wrote {len(content)} bytes to {resolved}")
