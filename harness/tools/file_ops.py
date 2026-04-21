"""delete_file / move_file / copy_file — basic file operations."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation


@enforce_atomic_validation
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
        # Use consolidated atomic validation and delete operation
        return await self._atomic_validate_and_delete(
            config, path, check_scope=True, resolve_symlinks=False
        )


@enforce_atomic_validation
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
        is_valid_src, src_validated = await self._validate_atomic_path(config, source, require_exists=True, check_scope=True, resolve_symlinks=False)
        if not is_valid_src:
            return src_validated  # This is the ToolResult error
        src = src_validated
        
        # Use atomic validation for destination to prevent TOCTOU attacks
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await self._validate_atomic_path(config, destination, require_exists=False, check_scope=True, resolve_symlinks=False)
        if not is_valid_dst:
            return dst_validated  # This is a ToolResult error
        dst = dst_validated  # This is the validated path string

        # Validate parent directory atomically to prevent TOCTOU symlink attacks
        parent_dir = Path(dst).parent
        if str(parent_dir) != ".":  # Skip if parent is current directory
            is_valid_parent, parent_result = await self._validate_and_prepare_parent_directory(
                config, str(parent_dir), require_exists=False, check_scope=True, resolve_symlinks=False
            )
            if not is_valid_parent:
                # parent_result should be a ToolResult when is_valid_parent is False
                # Defensive check in case implementation changes
                if isinstance(parent_result, ToolResult):
                    return parent_result
                else:
                    return ToolResult(error=str(parent_result), is_error=True)

        try:
            os.rename(src, dst)  # Atomic operation on validated path strings
        except FileNotFoundError:
            # File was deleted/moved by another process after validation
            return ToolResult(
                error=f"Source file disappeared after validation: {src}",
                is_error=True
            )
        except OSError as exc:
            # Handle cross-device moves (EXDEV) with fallback to copy+delete
            if exc.errno == errno.EXDEV:
                try:
                    # Fallback: copy then delete source
                    shutil.copy2(src, dst)
                    os.unlink(src)
                    return ToolResult(output=f"Moved {src} -> {dst} (cross-device via copy+delete)")
                except OSError as copy_exc:
                    return ToolResult(
                        error=f"Cross-device move failed during fallback copy/delete: {copy_exc}",
                        is_error=True
                    )
                except Exception as copy_exc:
                    return ToolResult(
                        error=f"Unexpected error during cross-device move fallback: {copy_exc}",
                        is_error=True
                    )
            return ToolResult(error=f"Move failed: {exc}", is_error=True)
        return ToolResult(output=f"Moved {src} -> {dst}")


@enforce_atomic_validation
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
        is_valid_src, src_validated = await self._validate_atomic_path(config, source, require_exists=True, check_scope=True, resolve_symlinks=False)
        if not is_valid_src:
            return src_validated  # This is the ToolResult error
        src = src_validated
        
        # Use atomic validation for destination to prevent TOCTOU attacks
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await self._validate_atomic_path(config, destination, require_exists=False, check_scope=True, resolve_symlinks=False)
        if not is_valid_dst:
            return dst_validated  # This is a ToolResult error
        dst = dst_validated  # This is the validated path string
        
        # Validate parent directory atomically to prevent TOCTOU symlink attacks
        parent_dir = Path(dst).parent
        if str(parent_dir) != ".":  # Skip if parent is current directory
            is_valid_parent, parent_result = await self._validate_and_prepare_parent_directory(
                config, str(parent_dir), require_exists=False, check_scope=True, resolve_symlinks=False
            )
            if not is_valid_parent:
                # parent_result should be a ToolResult when is_valid_parent is False
                # Defensive check in case implementation changes
                if isinstance(parent_result, ToolResult):
                    return parent_result
                else:
                    return ToolResult(error=str(parent_result), is_error=True)
        
        # Proceed with the copy using async thread
        try:
            await asyncio.to_thread(shutil.copy2, src, dst)
        except FileNotFoundError:
            # File was deleted by another process after validation
            return ToolResult(
                error=f"Source file disappeared after validation: {src}",
                is_error=True
            )
        except OSError as exc:
            # Handle specific OS errors with user-friendly messages
            if exc.errno == errno.EXDEV:
                try:
                    # Fallback for cross-device copy: use shutil.copy2 directly
                    # asyncio.to_thread can fail with EXDEV for cross-device operations
                    # due to thread pool resource constraints or file descriptor handling
                    shutil.copy2(src, dst)
                    return ToolResult(output=f"Copied {src} -> {dst} (cross-device)")
                except OSError as copy_exc:
                    return ToolResult(
                        error=f"Cross-device copy failed: {copy_exc}",
                        is_error=True
                    )
                except Exception as copy_exc:
                    return ToolResult(
                        error=f"Unexpected error during cross-device copy: {copy_exc}",
                        is_error=True
                    )
            if exc.errno == errno.ENOSPC:
                return ToolResult(
                    error=f"Cannot copy '{src}' to '{dst}': disk full (ENOSPC).",
                    is_error=True
                )
            return ToolResult(error=f"Copy failed: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(error=f"Copy failed: {exc}", is_error=True)
        
        return ToolResult(output=f"Copied {src} -> {dst}")
