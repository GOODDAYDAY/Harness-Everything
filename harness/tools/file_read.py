"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation


@enforce_atomic_validation
class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read the contents of a file. Supports offset (line number to start from, "
        "1-based) and limit (max lines to read) for large files."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})
    
    # Maximum allowed lines to prevent resource exhaustion attacks
    MAX_READ_LINES = 10000

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
        
        # Validate offset and limit values
        if offset < 1:
            return ToolResult(error=f"offset must be ≥ 1, got {offset}", is_error=True)
        if limit < 1:
            return ToolResult(error=f"limit must be ≥ 1, got {limit}", is_error=True)
        if limit > self.MAX_READ_LINES:
            return ToolResult(
                error=f"limit exceeds maximum allowed lines ({self.MAX_READ_LINES}), got {limit}",
                is_error=True
            )

        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_path, path_validated = await self._validate_atomic_path(config, path, require_exists=True, check_scope=True, resolve_symlinks=True)
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        resolved = path_validated

        # Use the shared atomic read helper
        text, read_error = await self._atomic_read_text(config, resolved)
        if read_error is not None:
            return read_error
        lines = text.splitlines(keepends=True)

        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        total = len(lines)
        
        # Handle empty selection (when start >= total)
        if not selected:
            # Extract filename from resolved path
            filename = os.path.basename(resolved)
            header = f"[{filename}] lines 0-0 of {total}\n"
            numbered = ""
            lines_metadata = []
        else:
            numbered = "".join(
                f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)
            )
            # Extract filename from resolved path
            filename = os.path.basename(resolved)
            header = f"[{filename}] lines {start+1}-{min(start+limit, total)} of {total}\n"
            
            # Create structured metadata with line numbers and content
            lines_metadata = [
                (start + i + 1, line) for i, line in enumerate(selected)
            ]
        
        return ToolResult(
            output=header + numbered,
            metadata={"lines": lines_metadata}
        )
