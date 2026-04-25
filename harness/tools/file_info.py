"""file_info — file metadata without reading content.

Returns line count, byte size, and last-modified time for one or more
files.  Critical for deciding how much to read (what ``limit`` to pass
to ``batch_read`` / ``read_file``) without wasting tokens on a full read.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class FileInfoTool(Tool):
    name = "file_info"
    description = (
        "Get file metadata (line count, byte size, last modified) WITHOUT "
        "reading the content. Use this BEFORE reading a file to decide what "
        "limit/offset to pass to batch_read or read_file. Accepts multiple "
        "paths — one round-trip for many files."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    MAX_PATHS = 100

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"File paths to inspect. Max {self.MAX_PATHS} per call."
                    ),
                },
            },
            "required": ["paths"],
        }

    async def _stat_one(self, config: HarnessConfig, raw_path: str) -> str:
        """Stat one file and return a formatted line."""
        if not raw_path:
            return f"  ERROR  {raw_path!r}: empty path"

        # Validate path security using the standard _check_path
        path_result = self._check_path(config, raw_path, require_exists=True)
        if isinstance(path_result, ToolResult):
            return f"  ERROR  {raw_path}: {path_result.error}"

        resolved = path_result  # validated resolved path string
        try:
            st = await asyncio.to_thread(os.stat, resolved)
        except OSError as exc:
            return f"  ERROR  {raw_path}: {exc}"

        if not (st.st_mode & 0o170000 == 0o100000):  # S_ISREG
            return f"  ERROR  {raw_path}: not a regular file"

        byte_size = st.st_size
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))

        # Count lines without reading entire file into memory
        try:
            line_count = await asyncio.to_thread(self._count_lines, resolved)
        except OSError as exc:
            return f"  ERROR  {raw_path}: cannot count lines: {exc}"

        # Format: size right-aligned, lines right-aligned
        return f"  {line_count:>6} lines  {byte_size:>9} bytes  {mtime}  {raw_path}"

    @staticmethod
    def _count_lines(path: str) -> int:
        """Count newlines in a file efficiently."""
        count = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                count += chunk.count(b"\n")
        return count

    async def execute(
        self, config: HarnessConfig, *, paths: list[str]
    ) -> ToolResult:
        if not paths:
            return ToolResult(error="paths must be a non-empty list", is_error=True)
        if len(paths) > self.MAX_PATHS:
            return ToolResult(
                error=f"Too many paths ({len(paths)}); max is {self.MAX_PATHS}",
                is_error=True,
            )

        lines = await asyncio.gather(*(
            self._stat_one(config, p) for p in paths
        ))

        header = f"File info for {len(paths)} path(s):\n"
        header += f"  {'lines':>6}        {'bytes':>9}       {'modified':>16}  path\n"
        header += f"  {'─' * 6}        {'─' * 9}       {'─' * 16}  {'─' * 20}\n"
        output = header + "\n".join(lines)
        return ToolResult(output=output)
