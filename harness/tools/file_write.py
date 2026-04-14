"""write_file — create or overwrite a file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create a new file or completely overwrite an existing file with the given content."
    requires_path_check = True

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
        resolved = str(Path(path).resolve())
        if err := self._check_path(config, resolved):
            return err

        p = Path(resolved)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as exc:
            return ToolResult(error=str(exc), is_error=True)

        return ToolResult(output=f"Wrote {len(content)} bytes to {resolved}")
