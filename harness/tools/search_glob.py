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
        "Returns matching file paths (relative to the root) sorted by "
        "modification time."
    )
    requires_path_check = True
    tags = frozenset({"search"})

    # Hard upper bound on candidates we inspect before filtering. A glob like
    # `**/*` in a large tree can otherwise return hundreds of thousands of
    # entries and pin CPU while we stat+resolve each one.
    MAX_CANDIDATES = 5000

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
        # _check_path: security-validated, symlink-resolved absolute path.
        checked = self._check_path(config, raw)
        if isinstance(checked, ToolResult):
            return checked
        root = Path(checked)

        # Resolve the allow-list once for per-match validation below.
        allowed = [
            Path(p).resolve(strict=False) for p in config.allowed_paths
        ] or [root]

        # Enumerate, cap candidates, then filter out anything that (after
        # symlink resolution) escapes the allow-list. This catches the case
        # where a symlink inside the workspace points to an external path —
        # glob would happily return that link target without this check.
        candidates: list[Path] = []
        capped = False
        for i, m in enumerate(root.glob(pattern)):
            if i >= self.MAX_CANDIDATES:
                capped = True
                break
            candidates.append(m)

        safe: list[tuple[Path, float]] = []
        for m in candidates:
            if not m.is_file():
                continue
            try:
                resolved = m.resolve()
            except OSError:
                continue
            if not any(
                resolved == a or resolved.is_relative_to(a) for a in allowed
            ):
                continue
            try:
                mtime = m.stat().st_mtime
            except OSError:
                mtime = 0.0
            safe.append((m, mtime))

        safe.sort(key=lambda kv: kv[1], reverse=True)
        truncated = len(safe) > limit
        safe = safe[:limit]

        if not safe:
            msg = f"No files match pattern '{pattern}' in {root}"
            if capped:
                msg += (
                    f"  (candidate scan stopped at {self.MAX_CANDIDATES}; "
                    "narrow the pattern)"
                )
            return ToolResult(output=msg)

        # Output relative paths — agents don't need the workspace absolute
        # prefix on every line, and relative paths are cheaper on context.
        lines: list[str] = []
        for m, _ in safe:
            try:
                rel = m.relative_to(root)
                lines.append(str(rel))
            except ValueError:
                lines.append(str(m))

        header = f"Found {len(lines)} file(s) matching '{pattern}':"
        if truncated:
            header += f"  (showing first {limit})"
        if capped:
            header += f"  (candidate scan stopped at {self.MAX_CANDIDATES}; some matches may be missing — narrow the pattern)"
        return ToolResult(output=header + "\n" + "\n".join(lines))
