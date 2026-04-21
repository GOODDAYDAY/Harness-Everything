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
        # Use consolidated atomic validation and write
        result = await self.file_security.atomic_validate_and_write(
            config, path, content, require_exists=False, check_scope=True, resolve_symlinks=False
        )
        # atomic_validate_and_write returns ToolResult (error or success)
        return result


