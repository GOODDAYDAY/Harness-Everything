"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

import asyncio
import errno
import os
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation, handle_atomic_result


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
            # First check if values are None (can happen with malformed JSON)
            if offset is None or limit is None:
                return ToolResult(
                    error=f"offset and limit cannot be None, got offset={offset!r} limit={limit!r}",
                    is_error=True,
                )
            
            # Attempt conversion to integer - try each separately for better error messages
            try:
                offset = int(offset)
            except (TypeError, ValueError) as offset_exc:
                # Provide specific error for offset conversion failure
                if isinstance(offset, str) and not offset.strip():
                    offset_desc = "empty string"
                elif isinstance(offset, str):
                    offset_desc = f"string '{offset}'"
                else:
                    offset_desc = f"{type(offset).__name__} {offset!r}"
                return ToolResult(
                    error=f"offset must be an integer, got {offset_desc}: {offset_exc}",
                    is_error=True,
                )
            
            try:
                limit = int(limit)
            except (TypeError, ValueError) as limit_exc:
                # Provide specific error for limit conversion failure
                if isinstance(limit, str) and not limit.strip():
                    limit_desc = "empty string"
                elif isinstance(limit, str):
                    limit_desc = f"string '{limit}'"
                else:
                    limit_desc = f"{type(limit).__name__} {limit!r}"
                return ToolResult(
                    error=f"limit must be an integer, got {limit_desc}: {limit_exc}",
                    is_error=True,
                )
        except Exception as exc:
            # Catch any other unexpected exceptions
            return ToolResult(
                error=f"Unexpected error converting offset/limit to integers: {exc}",
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
        # Use centralized handler for atomic validation results
        result = handle_atomic_result(atomic_result, metadata_keys=("text", "resolved_path"))
        if result.is_error:
            return result
        # Extract data from successful result
        text = result.metadata["text"]
        resolved = result.metadata["resolved_path"]
        

        lines = text.splitlines(keepends=True)
        total = len(lines)
        
        # Validate that offset is within file bounds
        # Offset must be ≤ total+1 lines (1-based indexing, allowing offset=1 for empty files)
        if offset > total + 1 or (total == 0 and offset > 1):
            filename = os.path.basename(resolved)
            return ToolResult(
                error=f"Offset {offset} exceeds file length ({total} lines) in {filename}",
                is_error=True
            )
        
        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        
        # Handle empty file case (total == 0)
        if total == 0 and offset == 1:
            # For empty files, only offset=1 is valid (already validated above)
            # Return consistent header format matching empty selection case
            filename = os.path.basename(resolved)
            header = f"[{filename}] lines 1-0 of 0\n"
            return ToolResult(
                output=header,
                metadata={"lines": []}
            )
        # Handle empty selection (when start >= total for non-empty files)
        elif not selected:
            # Extract filename from resolved path
            filename = os.path.basename(resolved)
            header = f"[{filename}] lines {offset}-{offset-1} of {total}\n"
            numbered = ""
            lines_metadata = []  # Empty selection: no lines returned
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
