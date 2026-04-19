"""delete_file / move_file / copy_file — basic file operations."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file."
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, *, path: str) -> ToolResult:
        # Use _check_path with standardized validation
        path_result = self._check_path(config, path)
        is_valid, validated = self._validate_path_result(path_result)
        if not is_valid:
            return validated  # This is a ToolResult error
        resolved = validated  # This is the validated path string
        if scope_err := self._check_phase_scope(config, resolved):
            return scope_err

        p = Path(resolved)
        if not p.exists():
            return ToolResult(error=f"Not found: {resolved}", is_error=True)
        p.unlink()
        return ToolResult(output=f"Deleted {resolved}")


class MoveFileTool(Tool):
    name = "move_file"
    description = "Move or rename a file."
    requires_path_check = True
    tags = frozenset({"file_write"})

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
        # FIX: Use _check_path instead of _validate_root_path directly
        src_result = self._check_path(config, source)
        # Add defensive assertion to catch type contract violations
        assert isinstance(src_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(src_result)}"
        if isinstance(src_result, ToolResult):
            return src_result  # This is a security or validation error
        src = src_result  # This is the validated path string
        
        dst_result = self._check_path(config, destination)
        # Add defensive assertion to catch type contract violations
        assert isinstance(dst_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(dst_result)}"
        if isinstance(dst_result, ToolResult):
            return dst_result  # This is a security or validation error
        dst = dst_result  # This is the validated path string
        # Scope check both source (we're removing it) and destination (we're
        # creating it) — a move out of scope is effectively both a delete and
        # a write.
        if scope_err := self._check_phase_scope(config, src):
            return scope_err
        if scope_err := self._check_phase_scope(config, dst):
            return scope_err

        if not Path(src).exists():
            return ToolResult(error=f"Source not found: {src}", is_error=True)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
        return ToolResult(output=f"Moved {src} -> {dst}")


class CopyFileTool(Tool):
    name = "copy_file"
    description = "Copy a file to a new location."
    requires_path_check = True
    tags = frozenset({"file_write"})

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
        # FIX: Use _check_path instead of _validate_root_path directly
        src_result = self._check_path(config, source)
        # Add defensive assertion to catch type contract violations
        assert isinstance(src_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(src_result)}"
        if isinstance(src_result, ToolResult):
            return src_result  # This is a security or validation error
        src = src_result  # This is the validated path string
        
        dst_result = self._check_path(config, destination)
        # Add defensive assertion to catch type contract violations
        assert isinstance(dst_result, (str, ToolResult)), f"Unexpected type from _check_path: {type(dst_result)}"
        if isinstance(dst_result, ToolResult):
            return dst_result  # This is a security or validation error
        dst = dst_result  # This is the validated path string
        # Scope check on destination only — copying out of scope is still a
        # write; reading the source does not create new state.
        if scope_err := self._check_phase_scope(config, dst):
            return scope_err

        if not Path(src).is_file():
            return ToolResult(error=f"Source not found: {src}", is_error=True)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return ToolResult(output=f"Copied {src} -> {dst}")
