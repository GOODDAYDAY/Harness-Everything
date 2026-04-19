"""list_directory / create_directory / tree — directory operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files and subdirectories in a directory, with type and size info."
    requires_path_check = True
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, *, path: str) -> ToolResult:
        # FIX: Use _check_path instead of _validate_root_path directly
        path_result = self._check_path(config, path)
        # Add defensive assertion to catch type contract violations
        assert isinstance(path_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(path_result)}"
        if isinstance(path_result, ToolResult):
            return path_result  # This is a security or validation error
        resolved = path_result  # This is the validated path string
        
        p = Path(resolved)
        if not p.is_dir():
            return ToolResult(error=f"Not a directory: {resolved}", is_error=True)

        lines: list[str] = []
        for entry in sorted(p.iterdir()):
            if entry.is_dir():
                lines.append(f"  [dir]  {entry.name}/")
            else:
                size = entry.stat().st_size
                lines.append(f"  {size:>8}  {entry.name}")
        return ToolResult(output=f"{resolved}/\n" + "\n".join(lines))


class CreateDirectoryTool(Tool):
    name = "create_directory"
    description = "Create a directory (and any missing parents)."
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, *, path: str) -> ToolResult:
        # FIX: Use _check_path instead of _validate_root_path directly
        path_result = self._check_path(config, path)
        # Add defensive assertion to catch type contract violations
        assert isinstance(path_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(path_result)}"
        if isinstance(path_result, ToolResult):
            return path_result  # This is a security or validation error
        resolved = path_result  # This is the validated path string
        
        Path(resolved).mkdir(parents=True, exist_ok=True)
        return ToolResult(output=f"Created {resolved}")


class TreeTool(Tool):
    name = "tree"
    description = (
        "Show directory structure as a tree. "
        "max_depth controls how deep to recurse (default: 3)."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory"},
                "max_depth": {
                    "type": "integer",
                    "description": "Max recursion depth (default: 3)",
                    "default": 3,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, config: HarnessConfig, *, path: str, max_depth: int = 3
    ) -> ToolResult:
        resolved, err = self._validate_root_path(config, path)
        if err:
            return err
        root = Path(resolved)
        if not root.is_dir():
            return ToolResult(error=f"Not a directory: {resolved}", is_error=True)

        lines: list[str] = [f"{root.name}/"]
        self._walk(root, "", max_depth, 0, lines)
        return ToolResult(output="\n".join(lines))

    def _walk(
        self,
        directory: Path,
        prefix: str,
        max_depth: int,
        depth: int,
        lines: list[str],
    ) -> None:
        if depth >= max_depth:
            return
        entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        # Filter hidden entries before computing connectors so that the last
        # *visible* entry correctly receives the "`-- " (end) connector instead
        # of "|-- " (continue).  Using the raw enumerate index from the
        # unfiltered list was a bug when hidden files appeared at the end.
        visible = [e for e in entries if not e.name.startswith(".")]
        for i, entry in enumerate(visible):
            is_last = i == len(visible) - 1
            connector = "`-- " if is_last else "|-- "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if is_last else "|   "
                self._walk(entry, prefix + extension, max_depth, depth + 1, lines)
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
