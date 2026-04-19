"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read the contents of a file. Supports offset (line number to start from, "
        "1-based) and limit (max lines to read) for large files."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "offset": {
                    "type": "integer",
                    "description": "Start reading from this line (1-based). Default: 1",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of lines to read. Default: 2000",
                    "default": 2000,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, config: HarnessConfig, *, path: str, offset: int = 1, limit: int = 2000
    ) -> ToolResult:
        # The Anthropic API occasionally delivers JSON integers as strings when
        # the LLM emits a quoted value (e.g. offset="2").  Coerce defensively so
        # callers get a clear error instead of a confusing TypeError deep inside
        # arithmetic on line 57.
        try:
            offset = int(offset)
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                error=f"offset and limit must be integers, got offset={offset!r} limit={limit!r}: {exc}",
                is_error=True,
            )

        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_path, path_validated = await self._validate_atomic_path(config, path)
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        resolved = path_validated

        # Use the atomic fallback helper from base class
        fd, error = await asyncio.to_thread(self._open_with_atomic_fallback, resolved, os.O_RDONLY)
        if error is not None:
            return error
        
        try:
            # Read file content from the file descriptor
            with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines(keepends=True)
        except Exception as exc:
            return ToolResult(error=f"Failed to read file: {exc}", is_error=True)

        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        numbered = "".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)
        )
        total = len(lines)
        header = f"[{p.name}] lines {start+1}-{min(start+limit, total)} of {total}\n"
        return ToolResult(output=header + numbered)
