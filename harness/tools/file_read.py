"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

import asyncio
import errno
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

        # Combined atomic validation and read
        atomic_result = await self.file_security.atomic_validate_and_read(
            config, path, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        if isinstance(atomic_result, ToolResult):
            return atomic_result  # Error from validation or read
        text, resolved = atomic_result
        

        lines = text.splitlines(keepends=True)
        total = len(lines)
        
        # Validate that offset is within file bounds
        # For empty files (total == 0), only offset=1 is allowed
        # For non-empty files, offset must be ≤ total lines
        if total == 0 and offset > 1:
            return ToolResult(
                error=f"Offset {offset} exceeds file length (file is empty)",
                is_error=True
            )
        if total > 0 and offset > total:
            filename = os.path.basename(resolved)
            return ToolResult(
                error=f"Offset {offset} exceeds file length ({total} lines) in {filename}",
                is_error=True
            )
        
        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        
        # Handle empty selection (when start >= total)
        if not selected:
            # Extract filename from resolved path
            filename = os.path.basename(resolved)
            header = f"[{filename}] lines {offset}-{offset-1} of {total}\n"
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
