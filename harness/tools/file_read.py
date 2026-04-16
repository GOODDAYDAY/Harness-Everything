"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

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

        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err

        p = Path(resolved)
        if not p.exists():
            return ToolResult(error=f"File not found: {resolved}", is_error=True)
        if not p.is_file():
            return ToolResult(error=f"Not a file: {resolved}", is_error=True)

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
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
