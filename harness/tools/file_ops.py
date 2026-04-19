"""delete_file / move_file / copy_file — basic file operations."""

from __future__ import annotations

import asyncio
import errno
import os
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
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_src, src_validated = await self._validate_atomic_path(config, path)
        if not is_valid_src:
            return src_validated  # This is the ToolResult error
        resolved = src_validated
        if scope_err := self._check_phase_scope(config, resolved):
            return scope_err

        # Atomic deletion without a separate existence check
        try:
            os.unlink(resolved)  # Atomic operation on the validated path string
        except FileNotFoundError:
            # File was deleted by another process after validation
            return ToolResult(
                error=f"File disappeared after validation: {resolved}",
                is_error=True
            )
        except OSError as exc:
            return ToolResult(error=f"Delete failed: {exc}", is_error=True)
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
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_src, src_validated = await self._validate_atomic_path(config, source)
        if not is_valid_src:
            return src_validated  # This is the ToolResult error
        src = src_validated
        
        # Use atomic validation for destination to prevent TOCTOU attacks
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await self._validate_atomic_path(config, destination, require_exists=False)
        if not is_valid_dst:
            return dst_validated  # This is a ToolResult error
        dst = dst_validated  # This is the validated path string
        
        # Scope check both source (we're removing it) and destination (we're
        # creating it) — a move out of scope is effectively both a delete and
        # a write.
        if scope_err := self._check_phase_scope(config, src):
            return scope_err
        if scope_err := self._check_phase_scope(config, dst):
            return scope_err

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dst)  # Atomic operation on validated path strings
        except FileNotFoundError:
            # File was deleted/moved by another process after validation
            return ToolResult(
                error=f"Source file disappeared after validation: {src}",
                is_error=True
            )
        except OSError as exc:
            # Handle cross-device moves (EXDEV) and other OS errors
            if exc.errno == errno.EXDEV:
                return ToolResult(
                    error=f"Cannot move '{src}' to '{dst}': cross-device move not supported. Use separate copy and delete operations.",
                    is_error=True
                )
            return ToolResult(error=f"Move failed: {exc}", is_error=True)
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
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_src, src_validated = await self._validate_atomic_path(config, source)
        if not is_valid_src:
            return src_validated  # This is the ToolResult error
        src = src_validated
        
        # Use atomic validation for destination to prevent TOCTOU attacks
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await self._validate_atomic_path(config, destination, require_exists=False)
        if not is_valid_dst:
            return dst_validated  # This is a ToolResult error
        dst = dst_validated  # This is the validated path string
        
        # Scope check on destination only — copying out of scope is still a
        # write; reading the source does not create new state.
        if scope_err := self._check_phase_scope(config, dst):
            return scope_err
        
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        
        # Proceed with the copy using async thread
        try:
            await asyncio.to_thread(shutil.copy2, src, dst)
        except Exception as exc:
            return ToolResult(error=f"Copy failed: {exc}", is_error=True)
        
        return ToolResult(output=f"Copied {src} -> {dst}")
