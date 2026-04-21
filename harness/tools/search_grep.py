"""grep_search — search file contents with regex."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class GrepSearchTool(Tool):
    name = "grep_search"
    description = (
        "Search file contents using a regex pattern. "
        "Returns matching lines with file paths (relative to root) and "
        "line numbers. Supports filtering by file glob (e.g. '*.py')."
    )
    requires_path_check = True
    tags = frozenset({"search"})

    # Hard cap on files scanned to keep `**/*` regex searches bounded on
    # huge repos. Files beyond this are skipped with a header note.
    MAX_GLOB_FILES = 5000

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
        # _check_path: symlink-resolved + allowed_paths-validated start point.
        checked = self._check_path(config, raw)
        if isinstance(checked, ToolResult):
            return checked
        root = Path(checked)

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult(error=f"Invalid regex: {exc}", is_error=True)

        # Resolve allow-list once so per-file validation is cheap.
        allowed = [
            Path(p).resolve(strict=False) for p in config.allowed_paths
        ] or [root]

        # Collect files. Per-file `resolve() ∈ allowed` filter catches symlinks
        # inside the workspace that point outside it (otherwise grep would
        # happily read them).
        files: list[Path] = []
        capped = False
        if root.is_file():
            files = [root]
        else:
            glob_pat = file_glob or "**/*"
            for i, f in enumerate(root.glob(glob_pat)):
                if i >= self.MAX_GLOB_FILES:
                    capped = True
                    break
                if not f.is_file():
                    continue
                try:
                    r = f.resolve()
                except OSError:
                    continue
                if any(r == a or r.is_relative_to(a) for a in allowed):
                    files.append(f)
            files.sort()

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
            msg = f"No matches for /{pattern}/ in {root}"
            if capped:
                msg += (
                    f"  (file scan stopped at {self.MAX_GLOB_FILES}; "
                    "narrow with file_glob or path)"
                )
            return ToolResult(output=msg)

        header = f"Found {total} match(es) for /{pattern}/:"
        if capped:
            header += f"  (file scan stopped at {self.MAX_GLOB_FILES}; narrow file_glob to see beyond)"
        return ToolResult(output=header + "\n" + "\n".join(results))
