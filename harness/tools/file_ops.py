"""delete_file / move_file / copy_file — basic file operations."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file."
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, *, path: str) -> ToolResult:
        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err
        p = Path(resolved)
        if not p.exists():
            return ToolResult(error=f"Not found: {resolved}", is_error=True)
        p.unlink()
        return ToolResult(output=f"Deleted {resolved}")


class MoveFileTool(Tool):
    name = "move_file"
    description = "Move or rename a file."
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source file path"},
                "destination": {"type": "string", "description": "Destination path"},
            },
            "required": ["source", "destination"],
        }

    async def execute(
        self, config: HarnessConfig, *, source: str, destination: str
    ) -> ToolResult:
        src, err = self._resolve_and_check(config, source)
        if err:
            return err
        dst, err = self._resolve_and_check(config, destination)
        if err:
            return err
        if not Path(src).exists():
            return ToolResult(error=f"Source not found: {src}", is_error=True)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
        return ToolResult(output=f"Moved {src} -> {dst}")


class CopyFileTool(Tool):
    name = "copy_file"
    description = "Copy a file to a new location."
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source file path"},
                "destination": {"type": "string", "description": "Destination path"},
            },
            "required": ["source", "destination"],
        }

    async def execute(
        self, config: HarnessConfig, *, source: str, destination: str
    ) -> ToolResult:
        src, err = self._resolve_and_check(config, source)
        if err:
            return err
        dst, err = self._resolve_and_check(config, destination)
        if err:
            return err
        if not Path(src).is_file():
            return ToolResult(error=f"Source not found: {src}", is_error=True)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return ToolResult(output=f"Copied {src} -> {dst}")
