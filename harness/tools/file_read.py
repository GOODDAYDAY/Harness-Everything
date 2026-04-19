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

        # FIX: Use _check_path instead of _validate_root_path directly
        path_result = self._check_path(config, path)
        # Handle type contract violations properly
        if not isinstance(path_result, (str, ToolResult)):
            return ToolResult(error=f"Unexpected type from _check_path: {type(path_result)}", is_error=True)
        if isinstance(path_result, ToolResult):
            return path_result  # This is a security or validation error
        resolved = path_result  # This is the validated path string

        p = Path(resolved)
        if not p.exists():
            return ToolResult(error=f"File not found: {resolved}", is_error=True)
        if not p.is_file():
            return ToolResult(error=f"Not a file: {resolved}", is_error=True)

        try:
            # Use asyncio.to_thread for async-safe file opening with O_NOFOLLOW
            # to prevent symlink swapping attacks (TOCTOU vulnerability)
            fd = await asyncio.to_thread(os.open, resolved, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                # Read file content from the file descriptor
                with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.read().splitlines(keepends=True)
            finally:
                # Ensure file descriptor is closed even if read fails
                try:
                    os.close(fd)
                except OSError:
                    pass  # Already closed by fdopen
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return ToolResult(
                    error=f"Symlink resolution escapes allowed directory: {resolved}",
                    is_error=True
                )
            elif exc.errno == errno.EINVAL:
                # O_NOFOLLOW not supported on this system - use stat with follow_symlinks=False
                try:
                    stat_result = await asyncio.to_thread(os.stat, resolved, follow_symlinks=False)
                    if not stat.S_ISREG(stat_result.st_mode):
                        return ToolResult(error=f"Not a regular file (may be a symlink): {resolved}", is_error=True)
                    # Safe to read after verifying it's not a symlink
                    # Note: There's still a small TOCTOU window between stat and read
                    lines = Path(resolved).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                except Exception as fallback_exc:
                    return ToolResult(error=f"Secure fallback failed: {fallback_exc}", is_error=True)
            else:
                # For other OSErrors (permission denied, etc.), return the error
                return ToolResult(error=f"Failed to open file with O_NOFOLLOW: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(error=str(exc), is_error=True)

        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        numbered = "".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)
        )
        total = len(lines)
        header = f"[{p.name}] lines {start+1}-{min(start+limit, total)} of {total}\n"
        return ToolResult(output=header + numbered)
