"""grep_search — search file contents with regex."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class GrepSearchTool(Tool):
    name = "grep_search"
    description = (
        "Search file contents using a regex pattern. "
        "Returns matching lines with file paths and line numbers. "
        "Supports filtering by file glob (e.g. '*.py')."
    )
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default: workspace)",
                    "default": "",
                },
                "file_glob": {
                    "type": "string",
                    "description": "Only search files matching this glob (e.g. '*.py')",
                    "default": "",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)",
                    "default": False,
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before and after each match (default: 0)",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max total matches to return (default: 100)",
                    "default": 100,
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
        file_glob: str = "",
        case_insensitive: bool = False,
        context_lines: int = 0,
        limit: int = 100,
    ) -> ToolResult:
        raw = path if path else config.workspace
        resolved, err = self._resolve_and_check(config, raw)
        if err:
            return err
        root = Path(resolved)

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(error=f"Invalid regex: {exc}", is_error=True)

        # collect files
        if root.is_file():
            files = [root]
        else:
            glob_pat = file_glob or "**/*"
            files = sorted(f for f in root.glob(glob_pat) if f.is_file())

        results: list[str] = []
        total = 0
        for fpath in files:
            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue

            for i, line in enumerate(lines):
                if regex.search(line):
                    if total >= limit:
                        break
                    rel = fpath.relative_to(root) if root.is_dir() else fpath.name
                    # context window
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    if context_lines > 0:
                        block = "\n".join(
                            f"  {j+1:>5}: {lines[j]}" for j in range(start, end)
                        )
                        results.append(f"{rel}:{i+1}\n{block}")
                    else:
                        results.append(f"{rel}:{i+1}: {line.rstrip()}")
                    total += 1
            if total >= limit:
                break

        if not results:
            return ToolResult(output=f"No matches for /{pattern}/ in {resolved}")

        header = f"Found {total} match(es) for /{pattern}/:\n"
        return ToolResult(output=header + "\n".join(results))
