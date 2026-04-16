"""glob_search — find files matching a glob pattern."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class GlobSearchTool(Tool):
    name = "glob_search"
    description = (
        "Search for files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Returns matching file paths sorted by modification time."
    )
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: workspace)",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 200)",
                    "default": 200,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        pattern: str,
        path: str = "",
        limit: int = 200,
    ) -> ToolResult:
        raw = path if path else config.workspace
        resolved, err = self._resolve_and_check(config, raw)
        if err:
            return err
        root = Path(resolved)

        matches = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        matches = [m for m in matches if m.is_file()][:limit]

        if not matches:
            return ToolResult(output=f"No files match pattern '{pattern}' in {resolved}")

        lines = [str(m) for m in matches]
        header = f"Found {len(lines)} file(s) matching '{pattern}':\n"
        return ToolResult(output=header + "\n".join(lines))
