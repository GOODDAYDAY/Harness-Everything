"""Abstract base for all tools."""

from __future__ import annotations

import asyncio
import errno
import fnmatch
import itertools
import json
import logging
import os  # Import for path operations; do not re-import in _check_path.
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional, Tuple, Union, TypeVar

from harness.core.config import HarnessConfig
from harness.core.security import validate_path_security

log = logging.getLogger(__name__)

# Type alias for atomic validation results
AtomicResult = Union[Tuple[str, str], "ToolResult"]
T = TypeVar("T")


def handle_atomic_result(result: AtomicResult, metadata_keys: Tuple[str, ...] = ("text", "resolved_path")) -> "ToolResult":
    """Handle the result from file_security.atomic_validate_and_* methods.
    
    This utility centralizes the duplicated type checking logic found across
    multiple file tools (file_read.py, file_edit.py, file_write.py, etc.).
    
    Args:
        result: Either a ToolResult (error) or a tuple of values
        metadata_keys: Names for the tuple elements when storing in metadata
        
    Returns:
        ToolResult: If input is a ToolResult, returns it unchanged.
                   If input is a tuple, returns a success ToolResult with the
                   tuple's data stored in metadata for later extraction.
    """
    if isinstance(result, ToolResult):
        return result  # Error case
    
    # Success case: tuple of values
    if len(result) != len(metadata_keys):
        return ToolResult(
            error=f"Tuple length {len(result)} doesn't match metadata_keys length {len(metadata_keys)}",
            is_error=True
        )
    
    metadata = dict(zip(metadata_keys, result))
    return ToolResult(
        output="",  # Empty output for success - actual content handled by caller
        metadata=metadata
    )


@dataclass
class ToolResult:
    """Uniform result returned by every tool execution."""

    output: str = ""
    error: str = ""
    is_error: bool = False
    elapsed_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        """Format as a tool_result content block for the Claude API."""
        text = self.error if self.is_error else self.output
        return {"type": "text", "text": text}


class FileSecurity:
    """Centralized security validation for file operations.
    
    This class consolidates all atomic validation methods to reduce duplication
    and ensure consistent security validation across all file tools.
    """
    
    @staticmethod
    async def atomic_validate_and_read(
        config: HarnessConfig,
        path: str,
        require_exists: bool = True,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> Tuple[str, str] | ToolResult:
        """Atomically validate and read a file with TOCTOU protection.
        
        Returns: (text, resolved_path) on success, ToolResult on error.
        """
        # Copy of the logic from Tool._atomic_validate_and_read
        validation_kwargs = {
            "require_exists": require_exists,
            "check_scope": check_scope,
            "resolve_symlinks": resolve_symlinks,
        }
        
        # Validate path atomically
        is_valid_path, path_validated = await FileSecurity.validate_atomic_path(config, path, **validation_kwargs)
        if not is_valid_path:
            # path_validated is a ToolResult when is_valid_path is False
            if isinstance(path_validated, ToolResult):
                return path_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(path_validated), is_error=True)
        
        # Read content atomically
        content, read_error = await FileSecurity._atomic_read_text(config, path_validated)
        if read_error is not None:
            return read_error
        
        return content, path_validated
    
    @staticmethod
    async def atomic_validate_and_write(
        config: HarnessConfig,
        path: str,
        content: str,
        require_exists: bool = False,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> ToolResult:
        """Atomically validate and write a file with TOCTOU protection."""
        # Copy of the logic from Tool._atomic_validate_and_write
        validation_kwargs = {
            "require_exists": require_exists,
            "check_scope": check_scope,
            "resolve_symlinks": resolve_symlinks,
        }
        
        # Validate path atomically
        is_valid_path, path_validated = await FileSecurity.validate_atomic_path(config, path, **validation_kwargs)
        if not is_valid_path:
            # path_validated is a ToolResult when is_valid_path is False
            if isinstance(path_validated, ToolResult):
                return path_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(path_validated), is_error=True)
        
        # Validate parent directory if needed
        parent_dir = os.path.dirname(path_validated)
        if parent_dir and parent_dir != ".":
            is_valid_parent, parent_result = await FileSecurity.validate_and_prepare_parent_directory(
                config, parent_dir,
                require_exists=False,
                check_scope=check_scope,
                resolve_symlinks=resolve_symlinks
            )
            if not is_valid_parent:
                # parent_result should be a ToolResult when is_valid_parent is False
                if isinstance(parent_result, ToolResult):
                    return parent_result
                else:
                    return ToolResult(error=str(parent_result), is_error=True)
        
        # Write content atomically
        try:
            # Use asyncio.to_thread to avoid blocking the event loop
            await asyncio.to_thread(lambda: open(path_validated, "w", encoding="utf-8").write(content))
            return ToolResult(output=f"Successfully wrote to {path_validated}")
        except OSError as exc:
            return ToolResult(error=f"Cannot write file {path_validated}: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(error=f"Unexpected error writing {path_validated}: {exc}", is_error=True)
    
    @staticmethod
    async def atomic_validate_and_delete(
        config: HarnessConfig,
        path: str,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> ToolResult:
        """Atomically validate and delete a file with TOCTOU protection."""
        # Copy of the logic from Tool._atomic_validate_and_delete
        # Validate path atomically
        is_valid_path, path_validated = await FileSecurity.validate_atomic_path(
            config, path,
            require_exists=True,
            check_scope=check_scope,
            resolve_symlinks=resolve_symlinks
        )
        if not is_valid_path:
            # path_validated is a ToolResult when is_valid_path is False
            if isinstance(path_validated, ToolResult):
                return path_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(path_validated), is_error=True)
        
        # Delete file atomically
        try:
            # Use asyncio.to_thread to avoid blocking the event loop
            await asyncio.to_thread(os.unlink, path_validated)
            return ToolResult(output=f"Successfully deleted {path_validated}")
        except FileNotFoundError:
            # File was deleted by another process after validation
            return ToolResult(
                error=f"File disappeared after validation: {path_validated}",
                is_error=True
            )
        except OSError as exc:
            return ToolResult(error=f"Cannot delete file {path_validated}: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(error=f"Unexpected error deleting {path_validated}: {exc}", is_error=True)
    
    @staticmethod
    async def validate_atomic_path(
        config: HarnessConfig,
        path: str,
        require_exists: bool = True,
        directory: bool = False,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> Tuple[bool, str | ToolResult]:
        """Atomically validate a file path with TOCTOU protection."""
        # Copy of the logic from Tool._validate_atomic_path
        try:
            # Run security checks (null bytes, homoglyphs) BEFORE any OS call.
            from harness.core.security import validate_path_security
            if err_msg := validate_path_security(path, config):
                return False, ToolResult(error=err_msg, is_error=True)

            # Resolve path relative to workspace
            resolved = os.path.join(config.workspace, path) if not os.path.isabs(path) else path

            # Check if path is within allowed scope
            if check_scope:
                from harness.core.security import validate_path_scope
                scope_ok, scope_error = await validate_path_scope(config, resolved)
                if not scope_ok:
                    return False, ToolResult(error=scope_error, is_error=True)
            
            # Check if file/directory exists if required
            if require_exists:
                if directory:
                    if not os.path.isdir(resolved):
                        return False, ToolResult(
                            error=f"Directory does not exist: {resolved}",
                            is_error=True
                        )
                else:
                    if not os.path.isfile(resolved):
                        return False, ToolResult(
                            error=f"File does not exist: {resolved}",
                            is_error=True
                        )
            
            # Check symlinks if not resolving them
            if not resolve_symlinks and os.path.islink(resolved):
                return False, ToolResult(
                    error=f"Symlinks are not allowed: {resolved}",
                    is_error=True
                )
            
            return True, resolved
            
        except Exception as exc:
            return False, ToolResult(error=f"Path validation failed: {exc}", is_error=True)
    
    @staticmethod
    async def validate_and_prepare_parent_directory(
        config: HarnessConfig,
        parent_dir: str,
        require_exists: bool = False,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> Tuple[bool, str | ToolResult]:
        """Atomically validate and prepare a parent directory."""
        # Copy of the logic from Tool._validate_and_prepare_parent_directory
        # First validate the parent directory path
        is_valid, validated = await FileSecurity.validate_atomic_path(
            config, parent_dir,
            require_exists=require_exists,
            directory=True,
            check_scope=check_scope,
            resolve_symlinks=resolve_symlinks
        )
        if not is_valid:
            return False, validated  # validated is a ToolResult
        
        # Create directory if it doesn't exist and require_exists is False
        if not require_exists and not os.path.exists(validated):
            try:
                os.makedirs(validated, exist_ok=True)
            except OSError as exc:
                return False, ToolResult(
                    error=f"Failed to create directory {validated}: {exc}",
                    is_error=True
                )
        
        return True, validated
    
    @staticmethod
    async def _atomic_read_text(
        config: HarnessConfig,
        resolved_path: str
    ) -> Tuple[str | None, ToolResult | None]:
        """Read file content atomically with TOCTOU protection.
        
        Returns: (text, None) on success, or (None, ToolResult) on error.
        """
        # Copy of the logic from Tool._atomic_read_text
        try:
            # Use asyncio.to_thread to avoid blocking the event loop
            content = await asyncio.to_thread(lambda: open(resolved_path, "r", encoding="utf-8").read())
            return content, None
        except FileNotFoundError:
            return None, ToolResult(
                error=f"File disappeared after validation: {resolved_path}",
                is_error=True
            )
        except UnicodeDecodeError:
            return None, ToolResult(
                error=f"File is not valid UTF-8 text: {resolved_path}",
                is_error=True
            )
        except OSError as exc:
            return None, ToolResult(
                error=f"Cannot read file {resolved_path}: {exc}",
                is_error=True
            )
        except Exception as exc:
            return None, ToolResult(
                error=f"Unexpected error reading {resolved_path}: {exc}",
                is_error=True
            )

    @staticmethod
    async def atomic_validate_and_move(
        config: HarnessConfig,
        source: str,
        destination: str,
        require_exists: bool = True,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> Tuple[str, str] | ToolResult:
        """Atomically validate source and destination paths for a move operation.
        
        Returns: (validated_source_path, validated_destination_path) on success,
                 ToolResult on error.
        """
        # Validate source path atomically
        validation_kwargs = {
            "require_exists": require_exists,
            "check_scope": check_scope,
            "resolve_symlinks": resolve_symlinks,
        }
        
        is_valid_src, src_validated = await FileSecurity.validate_atomic_path(config, source, **validation_kwargs)
        if not is_valid_src:
            # src_validated is a ToolResult when is_valid_src is False
            if isinstance(src_validated, ToolResult):
                return src_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(src_validated), is_error=True)
        
        # Validate destination path atomically
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await FileSecurity.validate_atomic_path(
            config, destination, require_exists=False, check_scope=check_scope, resolve_symlinks=resolve_symlinks
        )
        if not is_valid_dst:
            # dst_validated is a ToolResult when is_valid_dst is False
            if isinstance(dst_validated, ToolResult):
                return dst_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(dst_validated), is_error=True)
        
        return src_validated, dst_validated

    @staticmethod
    async def atomic_validate_and_copy(
        config: HarnessConfig,
        source: str,
        destination: str,
        require_exists: bool = True,
        check_scope: bool = True,
        resolve_symlinks: bool = False
    ) -> Tuple[str, str] | ToolResult:
        """Atomically validate source and destination paths for a copy operation.
        
        Returns: (validated_source_path, validated_destination_path) on success,
                 ToolResult on error.
        """
        # Validate source path atomically
        validation_kwargs = {
            "require_exists": require_exists,
            "check_scope": check_scope,
            "resolve_symlinks": resolve_symlinks,
        }
        
        is_valid_src, src_validated = await FileSecurity.validate_atomic_path(config, source, **validation_kwargs)
        if not is_valid_src:
            # src_validated is a ToolResult when is_valid_src is False
            if isinstance(src_validated, ToolResult):
                return src_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(src_validated), is_error=True)
        
        # Validate destination path atomically
        # require_exists=False because destination may not exist yet
        is_valid_dst, dst_validated = await FileSecurity.validate_atomic_path(
            config, destination, require_exists=False, check_scope=check_scope, resolve_symlinks=resolve_symlinks
        )
        if not is_valid_dst:
            # dst_validated is a ToolResult when is_valid_dst is False
            if isinstance(dst_validated, ToolResult):
                return dst_validated
            else:
                # This shouldn't happen, but handle defensively
                return ToolResult(error=str(dst_validated), is_error=True)
        
        return src_validated, dst_validated


class Tool(ABC):
    """Base class for all harness tools.

    Subclasses must define *name*, *description*, and implement
    *input_schema()* and *execute()*.
    """

    name: str
    description: str

    # Set to True if this tool operates on file paths that should be checked
    # against allowed_paths in config.
    requires_path_check: bool = False

    # Tool categories for tag-based filtering.
    # Valid tags: "file_read", "file_write", "search", "git", "analysis",
    #             "execution", "network", "testing"
    tags: frozenset[str] = frozenset()
    
    # Centralized security validation for file operations
    file_security: ClassVar[type] = FileSecurity

    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Return JSON Schema for the tool input."""

    @abstractmethod
    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        """Run the tool and return a ToolResult."""

    def api_schema(self) -> dict[str, Any]:
        """Export as a tool definition for the Claude API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema(),
        }

    # ---- helpers ----

    def _check_path(
        self,
        config: HarnessConfig,
        path: str,
        require_exists: bool = False,
        resolve_symlinks: bool = True,  # noqa: ARG002 – reserved for future use
    ) -> str | ToolResult:
        """Validate a file path against security rules and allowed-paths scope.

        Delegates security validation and scope checking to ``_validate_root_path``
        so both codepaths share a single implementation.  After the scope check
        passes, an optional existence check is applied.

        Returns:
            str  – the resolved, validated path on success.
            ToolResult – an error result on any validation failure.

        Args:
            require_exists: When *True*, return an error if the resolved path
                does not exist on disk.  Defaults to *False* so that paths for
                files-to-be-created pass validation.
            resolve_symlinks: Reserved for API compatibility; the underlying
                ``_validate_root_path`` always resolves symlinks.
        """
        resolved, err = self._validate_root_path(config, path)
        if err is not None:
            return err

        if require_exists and not Path(resolved).exists():
            return ToolResult(
                error=f"Path not found: {resolved}",
                is_error=True,
            )

        return resolved

    def _validate_path_result(self, path_result: Any) -> tuple[bool, str | ToolResult]:
        """Standardize type checking for _check_path return values.
        
        Returns: (is_valid, validated_path_or_error)
        - is_valid=True: path_result is a string (validated path)
        - is_valid=False: path_result is a ToolResult (error)
        
        This helper eliminates the inconsistent type checking currently
        duplicated across tools (e.g., file_read.py lines 47-51).
        """
        if isinstance(path_result, str):
            return True, path_result
        elif isinstance(path_result, ToolResult):
            return False, path_result
        else:
            # This should never happen if _check_path is implemented correctly
            return False, ToolResult(
                error=f"Unexpected type from _check_path: {type(path_result)}",
                is_error=True,
            )

    def _validate_atomic_path_sync(
        self, config: HarnessConfig, path_str: str, require_exists: bool = True, 
        directory: bool = False, check_scope: bool = False, resolve_symlinks: bool = True
    ) -> tuple[bool, str | ToolResult]:
        """
        Synchronous atomic path validation with inode verification.
        
        Opens path with os.O_RDONLY | os.O_NOFOLLOW, validates via _check_path,
        and verifies file hasn't changed using st_dev and st_ino.
        
        Args:
            resolve_symlinks: If True, symlinks are resolved to their final target 
                before scope checking. If False, the symlink path itself is validated.
        
        Returns (is_valid, validated_path_str | ToolResult_error).
        """
        # 1. Use existing path validation
        path_result = self._check_path(config, path_str, require_exists=require_exists, resolve_symlinks=resolve_symlinks)
        is_valid, validated = self._validate_path_result(path_result)
        if not is_valid:
            return False, validated
        resolved = validated

        # 2. Atomic open with O_NOFOLLOW to prevent symlink traversal
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if directory:
            flags |= getattr(os, 'O_DIRECTORY', 0)  # O_DIRECTORY may not exist on all platforms
        
        try:
            fd = os.open(resolved, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return False, ToolResult(error=f"Symlinks are not allowed: {resolved}", is_error=True)
            elif exc.errno == errno.ENOENT:
                if require_exists:
                    return False, ToolResult(error=f"File not found: {resolved}", is_error=True)
                else:
                    # File/directory doesn't exist, but that's OK for create operations
                    # We've already validated the path is within allowed directories via _check_path
                    return True, resolved
            elif exc.errno == errno.ENOTDIR and directory:
                return False, ToolResult(error=f"Not a directory: {resolved}", is_error=True)
            elif exc.errno == errno.EINVAL and directory:
                # O_DIRECTORY not supported on this platform
                # Fall back to non-atomic check
                try:
                    if not os.path.isdir(resolved):
                        return False, ToolResult(error=f"Not a directory: {resolved}", is_error=True)
                except Exception as fallback_exc:
                    return False, ToolResult(error=f"Directory validation failed: {fallback_exc}", is_error=True)
                # fd was never opened in this case, so don't close it
                return True, resolved
            else:
                return False, ToolResult(error=f"Cannot access file: {exc}", is_error=True)
        
        try:
            # Get file stats to verify inode
            stat_info = os.fstat(fd)
            
            # Perform scope check while file descriptor is still open (TOCTOU fix)
            if check_scope:
                scope_error = self._check_phase_scope(config, resolved)
                if scope_error is not None:
                    os.close(fd)
                    return False, scope_error
            
            # Verify the file we have open is the same as what _check_path validated
            # by checking it's within allowed paths
            for allowed_path in config.allowed_paths:
                try:
                    # Try to find the file by inode within allowed path
                    found_path = self._find_path_by_inode(
                        stat_info.st_dev, stat_info.st_ino, resolved, allowed_path
                    )
                    # Verify found_path is within allowed_path
                    if os.path.commonpath([found_path, allowed_path]) == allowed_path:
                        os.close(fd)
                        return True, found_path
                except (OSError, ValueError):
                    continue
            
            # If we get here, file is not within any allowed path
            os.close(fd)
            return False, ToolResult(
                error="TOCTOU security violation: file path changed during validation - resolved path not within allowed directories",
                is_error=True
            )
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            return False, ToolResult(error=f"TOCTOU security violation: file validation failed - {exc}", is_error=True)

    async def _validate_atomic_path(
        self, config: HarnessConfig, path_str: str, require_exists: bool = True, 
        directory: bool = False, check_scope: bool = False, resolve_symlinks: bool = True
    ) -> tuple[bool, str | ToolResult]:
        """
        Atomically validate a path is accessible and is a regular file or directory.
        Returns (is_valid, validated_path_str | ToolResult_error).
        
        Args:
            resolve_symlinks: If True, symlinks are resolved to their final target 
                before scope checking. If False, the symlink path itself is validated.
        
        This async wrapper delegates to the synchronous implementation.
        """
        return await asyncio.to_thread(
            self._validate_atomic_path_sync, config, path_str, require_exists, directory, check_scope, resolve_symlinks
        )

    async def _validate_directory_atomic(
        self, config: HarnessConfig, path_str: str, resolve_symlinks: bool = True
    ) -> tuple[bool, str | ToolResult]:
        """
        Atomically validate a path is accessible and is a directory.
        Returns (is_valid, validated_path_str | ToolResult_error).
        """
        # Use the consolidated atomic path validation with directory flag
        return await self._validate_atomic_path(config, path_str, require_exists=True, directory=True, resolve_symlinks=resolve_symlinks)

    async def _validate_and_prepare_parent_directory(
        self, config: HarnessConfig, parent_path: str, require_exists: bool = True, 
        check_scope: bool = False, resolve_symlinks: bool = True
    ) -> tuple[bool, str | ToolResult]:
        """
        Atomically validate and optionally create a parent directory.
        
        Args:
            resolve_symlinks: If True, symlinks are resolved to their final target 
                before scope checking. If False, the symlink path itself is validated.
        
        Returns: tuple[bool, str | ToolResult] where:
            - First element is True if validation succeeded, False otherwise
            - Second element is validated path string on success, or ToolResult error on failure
            - Callers must check the type of the second element before using it
            - When first element is False, second element is guaranteed to be a ToolResult
        
        If require_exists=False and directory doesn't exist, it will be created.
        """
        # Skip if parent is current directory
        if parent_path == ".":
            return True, parent_path
            
        # Validate parent directory exists and is not a symlink
        is_valid_parent, parent_validated = await self._validate_atomic_path(
            config, parent_path, require_exists=require_exists, directory=True, 
            check_scope=check_scope, resolve_symlinks=resolve_symlinks
        )
        if not is_valid_parent:
            return is_valid_parent, parent_validated
            
        # Create parent directory if it doesn't exist and require_exists=False
        if not require_exists:
            try:
                import os
                os.makedirs(parent_validated, exist_ok=True)
            except OSError as exc:
                return False, ToolResult(error=f"Failed to create parent directory: {exc}", is_error=True)
                
        return True, parent_validated

    async def _validate_path_atomic(
        self, config: HarnessConfig, path: str
    ) -> tuple[bool, str | ToolResult]:
        """
        Unified atomic path validation method to eliminate TOCTOU vulnerabilities.
        
        Uses os.open with O_NOFOLLOW to atomically obtain a file descriptor,
        resolves the real path, and validates against allowed_paths.
        
        Returns (True, resolved_path_str) on success, or 
        (False, ToolResult(error=..., is_error=True)) on failure.
        """
        import errno
        
        # Use atomic open with O_NOFOLLOW to prevent symlink traversal
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return False, ToolResult(
                    error=f"Symlink loop detected or too many levels of symbolic links: {path}",
                    is_error=True
                )
            elif exc.errno == errno.ENOENT:
                return False, ToolResult(
                    error=f"File not found: {path}",
                    is_error=True
                )
            else:
                return False, ToolResult(
                    error=f"Cannot access file: {exc}",
                    is_error=True
                )
        
        try:
            # Try to get real path via /proc/self/fd on Linux
            resolved_path = None
            try:
                proc_path = f"/proc/self/fd/{fd}"
                if os.path.exists(proc_path):
                    resolved_path = os.path.realpath(proc_path)
            except (OSError, AttributeError):
                pass
            
            # Fallback to os.path.realpath if /proc method failed
            if not resolved_path:
                resolved_path = os.path.realpath(path)
            
            # Validate the resolved path against allowed_paths
            path_result = self._check_path(config, resolved_path)
            is_valid, validated = self._validate_path_result(path_result)
            
            if not is_valid:
                return False, validated
            
            # Additional security: verify the file we have open is within allowed paths
            # by checking inode against allowed directories
            stat_info = os.fstat(fd)
            for allowed_path in config.allowed_paths:
                try:
                    # Try to find the file by inode within allowed path
                    found_path = self._find_path_by_inode(
                        stat_info.st_dev, stat_info.st_ino, resolved_path, allowed_path
                    )
                    # Verify found_path is within allowed_path
                    if os.path.commonpath([found_path, allowed_path]) == allowed_path:
                        return True, found_path
                except (OSError, ValueError):
                    continue
            
            # If we get here, file is not within any allowed path
            return False, ToolResult(
                error="TOCTOU security violation: file path changed during validation - resolved path not within allowed directories",
                is_error=True
            )
            
        except Exception as exc:
            return False, ToolResult(
                error=f"Path validation failed: {exc}",
                is_error=True
            )
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _find_path_by_inode(
        self, dev: int, ino: int, original_path: str, search_root: str
    ) -> str:
        """
        Find a file by its device and inode numbers within a search root.
        
        This provides additional security by verifying the actual file
        (identified by inode) is within allowed directories.
        """
        import os

        # If the original path is already within search_root, use it
        try:
            if os.path.commonpath([original_path, search_root]) == search_root:
                return original_path
        except ValueError:
            pass
        
        # Walk the search root to find the file by inode
        for root, dirs, files in os.walk(search_root):
            for name in files + dirs:
                full_path = os.path.join(root, name)
                try:
                    stat_info = os.lstat(full_path)
                    if stat_info.st_dev == dev and stat_info.st_ino == ino:
                        return full_path
                except OSError:
                    continue
        
        # If not found, return the original path (validation will fail later)
        return original_path

    def _open_with_atomic_fallback(self, path: str, flags: int) -> tuple[int | None, ToolResult | None]:
        """
        Atomically open a file with fallback for systems without O_NOFOLLOW support.
        
        Returns (file_descriptor, None) on success, or (None, ToolResult_error) on failure.
        
        Security guarantee: The file type is verified atomically before returning.
        For systems without O_NOFOLLOW, we use O_PATH (Linux) or open+fstat (other)
        to verify file type before any operations.
        """
        try:
            # First attempt: open with O_NOFOLLOW to prevent symlink swapping
            fd = os.open(path, flags | os.O_NOFOLLOW)
            return fd, None
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                # O_NOFOLLOW not supported on this filesystem/platform
                # Use atomic fallback: open with O_PATH if available, otherwise regular open
                fallback_flags = flags
                if hasattr(os, 'O_PATH'):
                    # Linux: O_PATH allows opening without following symlinks
                    fallback_flags |= os.O_PATH
                
                try:
                    fd = os.open(path, fallback_flags)
                    try:
                        # Use fstat on open fd to verify file type atomically
                        stat_result = os.fstat(fd)
                        # Reject symlinks (important for O_PATH on Linux)
                        if stat.S_ISLNK(stat_result.st_mode):
                            os.close(fd)
                            return None, ToolResult(
                                error=f"Path is a symlink: {path}",
                                is_error=True
                            )
                        if not stat.S_ISREG(stat_result.st_mode):
                            os.close(fd)
                            return None, ToolResult(
                                error=f"Not a regular file: {path}",
                                is_error=True
                            )
                        # Success: file is regular, fd is valid
                        return fd, None
                    except Exception:
                        # Close fd on any fstat error
                        os.close(fd)
                        raise
                except OSError as fallback_exc:
                    if fallback_exc.errno == errno.ELOOP:
                        return None, ToolResult(
                            error=f"Symlink resolution escapes allowed directory: {path}",
                            is_error=True
                        )
                    return None, ToolResult(
                        error=f"Secure fallback failed: {fallback_exc}",
                        is_error=True
                    )
            elif exc.errno == errno.ELOOP:
                return None, ToolResult(
                    error=f"Symlink resolution escapes allowed directory: {path}",
                    is_error=True
                )
            else:
                return None, ToolResult(
                    error=f"Cannot access file: {exc}",
                    is_error=True
                )

    def _guaranteed_fd_cleanup(self, fd: int, operation: Callable[[int], Any]) -> Tuple[Any, Optional[ToolResult]]:
        """
        Execute `operation(fd)` and guarantee `os.close(fd)` is called on failure.
        Returns (result, None) on success, or (None, ToolResult) on failure.
        
        On success, ownership of `fd` is transferred to the result of `operation`.
        On failure, `fd` is closed before returning an error.
        """
        try:
            result = operation(fd)  # e.g., os.fdopen(fd, 'rb')
            return result, None
        except Exception as exc:
            # Close fd only on operation failure
            try:
                os.close(fd)
            except OSError:
                pass  # FD may already be closed; ignore secondary error
            return None, ToolResult(error=f"File operation failed on descriptor {fd}: {exc}", is_error=True)

    async def _validate_and_read_atomic(
        self, 
        config: HarnessConfig, 
        path: str, 
        **validation_kwargs
    ) -> Union[Tuple[str, str], ToolResult]:
        """
        Atomically validate a path and read its content.
        Returns (content, resolved_path) on success, or a ToolResult error.
        """
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_path, path_validated = await self._validate_atomic_path(config, path, **validation_kwargs)
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        
        # The validated path is now locked; read from the same file descriptor
        content, read_error = await self._atomic_read_text(config, path_validated)
        if read_error is not None:
            return read_error
        
        return content, path_validated

    async def _atomic_read_text(self, config: HarnessConfig, resolved_path: str) -> Tuple[str | None, ToolResult | None]:
        """
        Read file content atomically with TOCTOU protection.
        
        Encapsulates the common pattern used by EditFileTool and ReadFileTool:
        1. Validate path atomically
        2. Open file with atomic fallback
        3. Use guaranteed FD cleanup
        4. Read binary content and decode with error handling
        
        Returns:
            Tuple of (text, None) on success, or (None, ToolResult) on error.
        """
        import asyncio
        import os
        
        # Use atomic validation for source file to prevent TOCTOU attacks
        is_valid_path, path_validated = await self._validate_atomic_path(config, resolved_path)
        if not is_valid_path:
            return None, path_validated  # This is the ToolResult error
        
        # Use atomic file opening to prevent TOCTOU attacks
        fd, error = await asyncio.to_thread(self._open_with_atomic_fallback, path_validated, os.O_RDONLY)
        if error is not None:
            return None, error
        
        # Use the helper to safely convert fd to a file object
        def fdopen_operation(fd: int):
            return os.fdopen(fd, 'rb')
        
        file_obj, open_error = await asyncio.to_thread(self._guaranteed_fd_cleanup, fd, fdopen_operation)
        if open_error is not None:
            return None, open_error
        # file_obj is now guaranteed to be open, and the original fd is closed.
        
        try:
            # Read binary and decode with same error handling as original
            content = file_obj.read()
            text = content.decode('utf-8', errors='replace')
            return text, None
        except Exception as exc:
            return None, ToolResult(error=f"Failed to read file: {exc}", is_error=True)
        finally:
            file_obj.close()

    async def _atomic_validate_and_read(
        self,
        config: HarnessConfig,
        path: str,
        require_exists: bool = True,
        check_scope: bool = True,
        resolve_symlinks: bool = True
    ) -> Union[Tuple[str, str], ToolResult]:
        """Consolidated atomic validation and read operation.
        
        This method combines path validation and file reading into a single atomic
        operation to prevent TOCTOU vulnerabilities.
        
        Args:
            config: HarnessConfig instance
            path: Path to the file
            require_exists: Whether the file must exist
            check_scope: Whether to check if path is within allowed workspace
            resolve_symlinks: Whether to resolve symlinks
            
        Returns:
            Tuple of (text, resolved_path) on success, ToolResult error on failure
        """
        return await self._validate_and_read_atomic(
            config, path, 
            require_exists=require_exists,
            check_scope=check_scope,
            resolve_symlinks=resolve_symlinks
        )

    async def _atomic_validate_and_write(
        self,
        config: HarnessConfig,
        path: str,
        content: str,
        require_exists: bool = False,
        check_scope: bool = True,
        resolve_symlinks: bool = True
    ) -> ToolResult:
        """Consolidated atomic validation and write operation.
        
        This method combines path validation and file writing into a single atomic
        operation to prevent TOCTOU vulnerabilities.
        
        Args:
            config: HarnessConfig instance
            path: Path to the file
            content: Content to write
            require_exists: Whether the file must exist (False for write operations)
            check_scope: Whether to check if path is within allowed workspace
            resolve_symlinks: Whether to resolve symlinks
            
        Returns:
            ToolResult success or error
        """
        # Use atomic validation for target file
        is_valid_path, path_validated = await self._validate_atomic_path(
            config, path, 
            require_exists=require_exists, 
            check_scope=check_scope, 
            resolve_symlinks=resolve_symlinks
        )
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        resolved = path_validated

        # Validate parent directory atomically
        parent_dir = Path(resolved).parent
        if str(parent_dir) != ".":  # Skip if parent is current directory
            is_valid_parent, parent_result = await self._validate_and_prepare_parent_directory(
                config, str(parent_dir), 
                require_exists=False,  # For writes, parent may not exist and should be created
                check_scope=False,  # Don't check parent directory against glob patterns - only check the file path
                resolve_symlinks=resolve_symlinks
            )
            if not is_valid_parent:
                return parent_result  # This is a ToolResult error

        # Write back using the async atomic helper
        write_error = await self._atomic_write_text(resolved, content)
        if write_error is not None:
            return write_error
        
        return ToolResult(output=f"Wrote {len(content)} bytes to {resolved}")

    async def _atomic_validate_and_delete(
        self,
        config: HarnessConfig,
        path: str,
        check_scope: bool = True,
        resolve_symlinks: bool = True
    ) -> ToolResult:
        """Consolidated atomic validation and delete operation.
        
        This method combines path validation and file deletion into a single atomic
        operation to prevent TOCTOU vulnerabilities.
        
        Args:
            config: HarnessConfig instance
            path: Path to the file to delete
            check_scope: Whether to check if path is within allowed workspace
            resolve_symlinks: Whether to resolve symlinks
            
        Returns:
            ToolResult success or error
        """
        # Use atomic validation for source file
        is_valid_path, path_validated = await self._validate_atomic_path(
            config, path, 
            require_exists=True,  # File must exist to delete it
            check_scope=check_scope, 
            resolve_symlinks=resolve_symlinks
        )
        if not is_valid_path:
            return path_validated  # This is the ToolResult error
        resolved = path_validated

        # Atomic deletion without a separate existence check
        try:
            await asyncio.to_thread(os.unlink, resolved)  # Atomic operation on the validated path string
        except FileNotFoundError:
            # File was deleted by another process after validation
            return ToolResult(
                error=f"File disappeared after validation: {resolved}",
                is_error=True
            )
        except OSError as exc:
            return ToolResult(error=f"Delete failed: {exc}", is_error=True)
        
        return ToolResult(output=f"Deleted {resolved}")

    async def _atomic_validate_parent(
        self,
        config: HarnessConfig,
        parent_path: str,
        require_exists: bool = True,
        check_scope: bool = True,
        resolve_symlinks: bool = True
    ) -> Union[Tuple[bool, str], ToolResult]:
        """Consolidated atomic parent directory validation.
        
        This method validates parent directories with atomic operations
        to prevent TOCTOU vulnerabilities.
        
        Args:
            config: HarnessConfig instance
            parent_path: Path to parent directory
            require_exists: Whether the directory must exist
            check_scope: Whether to check if path is within allowed workspace
            resolve_symlinks: Whether to resolve symlinks
            
        Returns:
            Tuple of (is_valid, resolved_path) on success, ToolResult error on failure
        """
        return await self._validate_and_prepare_parent_directory(
            config, parent_path,
            require_exists=require_exists,
            check_scope=check_scope,
            resolve_symlinks=resolve_symlinks
        )

    async def _atomic_write_text(self, resolved_path: str, content: str) -> ToolResult | None:
        """Atomically write content to resolved_path using temp file + os.replace.
        
        All disk I/O runs in asyncio.to_thread to avoid blocking the event loop.
        Returns None on success, ToolResult error on failure.
        """
        import tempfile
        import os
        
        def _sync_write():
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", 
                    dir=os.path.dirname(resolved_path) or ".", 
                    delete=False
                ) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                os.replace(tmp_path, resolved_path)
                return None
            except Exception as exc:
                # Clean up temp file if it exists
                try:
                    if tmp_path is not None and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
                return ToolResult(error=f"Failed to write file: {exc}", is_error=True)
        
        try:
            return await asyncio.to_thread(_sync_write)
        except Exception as exc:
            return ToolResult(error=f"Async write failed: {exc}", is_error=True)

    def _find_path_by_inode(self, st_dev: int, st_ino: int, original_path_hint: str, workspace_root: str) -> str:
        """
        Find a file by its device and inode numbers within the workspace.
        
        This is a fallback for systems without /proc/self/fd/ support.
        Walks the workspace directory tree looking for a file matching the
        given device and inode numbers.
        
        Args:
            st_dev: Device number from stat()
            st_ino: Inode number from stat()
            original_path_hint: Original path for fallback
            workspace_root: Root of workspace to search within
            
        Returns:
            Path to the file if found, otherwise os.path.realpath(original_path_hint)
        """
        try:
            for root, dirs, files in os.walk(workspace_root):
                for name in files:
                    filepath = os.path.join(root, name)
                    try:
                        stat_info = os.stat(filepath, follow_symlinks=False)
                        if stat_info.st_dev == st_dev and stat_info.st_ino == st_ino:
                            return filepath
                    except OSError:
                        continue  # Skip files we can't stat
        except Exception:
            # If walk fails for any reason, fall back to original path
            pass
        
        # Fallback: return the realpath of the original hint
        return os.path.realpath(original_path_hint)


    def _check_phase_scope(
        self, config: HarnessConfig, resolved_path: str,
    ) -> ToolResult | None:
        """Reject writes outside the running phase's allowed_edit_globs.

        Returns None when the path is allowed (including: no globs configured,
        i.e. back-compat unrestricted mode). Returns a ToolResult error when
        the write should be blocked. Callers should invoke this AFTER
        ``_check_path`` has resolved and security-validated the path, so the
        path here is absolute and known-safe.

        Matching is against the workspace-relative path using fnmatch
        semantics, with a ``**`` prefix expanded so e.g. ``harness/**`` also
        matches ``harness/core/hooks.py``. A path that is not under the
        workspace (rare, but allowed_paths may span multiple roots) is
        allowed — phase scoping is intentionally a workspace-local concept.

        Scoping is cheap: an empty list skips the entire check.
        """
        globs = getattr(config, "phase_edit_globs", None)
        if not globs:
            return None
        try:
            rel = os.path.relpath(resolved_path, config.workspace)
        except ValueError:
            # Cross-drive on Windows: path is outside workspace entirely.
            return None
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        rel_posix = rel.replace(os.sep, "/")
        for pattern in globs:
            if fnmatch.fnmatch(rel_posix, pattern):
                return None
            # Support "foo/**" as a recursive prefix match (fnmatch alone
            # treats '**' like '*').
            if pattern.endswith("/**"):
                prefix = pattern[:-3]
                if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                    return None
        return ToolResult(
            error=(
                f"PHASE SCOPE ERROR: path {rel_posix!r} is not in this "
                f"phase's allowed_edit_globs ({globs}). Edit a different "
                "file, or report this as out-of-scope and move on."
            ),
            is_error=True,
        )

    def _validate_root_path(self, config: HarnessConfig, root: str) -> tuple[str, ToolResult | None]:
        """Validate a root path for directory operations.
        
        Combines security validation from _check_path with path resolution
        and allowed paths checking. Returns (resolved_path, None) on success
        or ("", error_ToolResult) on failure.
        
        This method consolidates the logic previously duplicated in
        _resolve_and_check and _check_dir_root.
        """
        # Handle empty root (use workspace)
        path_to_check = root if root else config.workspace
        
        # If path is relative, join it with workspace
        if not os.path.isabs(path_to_check):
            path_to_check = os.path.join(config.workspace, path_to_check)
        
        # 1. Security validation on raw path
        if error_msg := validate_path_security(path_to_check, config):
            return "", ToolResult(
                error=error_msg,
                is_error=True,
            )
        
        # 2. Resolve path to eliminate symlink TOCTOU.
        # Use strict=True when the path exists (catches symlink targets outside
        # allowed dirs); fall back to strict=False for paths that don't exist
        # yet (e.g. output files to be created) without requiring parent dirs.
        try:
            resolved_path = Path(path_to_check).resolve(strict=True)
        except OSError:
            # Path doesn't exist yet — use non-strict resolution which resolves
            # as far as the path exists then appends remaining components.
            try:
                resolved_path = Path(path_to_check).resolve(strict=False)
            except Exception as exc2:
                return "", ToolResult(
                    error=f"Cannot resolve path {path_to_check!r}: {exc2}",
                    is_error=True,
                )
        resolved = str(resolved_path)
        
        # 3. Check if resolved path is within allowed_paths.
        # Resolve allowed_paths to handle OS-level symlinks (e.g. macOS /var →
        # /private/var) before comparing against the already-resolved path.
        allowed_resolved = [
            os.path.realpath(str(ap)) for ap in (config.allowed_paths or [])
        ]
        in_scope = any(
            resolved == ap or resolved.startswith(ap + os.sep)
            for ap in allowed_resolved
        )
        if not in_scope:
            return "", ToolResult(
                error=(
                    f"PERMISSION ERROR: Path not allowed: {resolved} is outside allowed "
                    f"directories (allowed: {config.allowed_paths})"
                ),
                is_error=True,
            )
        
        return resolved, None

    def _resolve_and_check(
        self, config: HarnessConfig, path: str
    ) -> tuple[str, ToolResult | None]:
        """Validate and resolve a file path.
        
        DEPRECATED: Use _check_path instead, which returns the resolved path
        directly or a ToolResult error.
        
        Uses the consolidated _validate_root_path method for security validation,
        path resolution, and allowed paths checking.
        
        Returns:
            (resolved_path, None) on success.
            ("", error_ToolResult) on any failure.
        """
        import warnings
        warnings.warn(
            "_resolve_and_check is deprecated, use _check_path instead",
            DeprecationWarning,
            stacklevel=2
        )
        return self._validate_root_path(config, path)

    def _check_dir_root(
        self,
        config: HarnessConfig,
        root: str,
    ) -> tuple[Path, list[Path], ToolResult | None]:
        """Validate *root* against allowed_paths.

        Returns (resolved_root, allowed_list, None) on success or
        (Path('.'), [], error_ToolResult) on failure.

        Returns the pre-resolved allowed list as the second element so
        callers never re-derive it (eliminates the asymmetry present in
        the inline copies in cross_reference.py and feature_search.py).

        Uses comprehensive security validation including homoglyph detection.
        """
        # Use the consolidated validation method
        resolved_path, err = self._validate_root_path(config, root)
        if err:
            return Path("."), [], err
        
        # Convert to Path and compute allowed list
        search_root = Path(resolved_path)
        allowed = [Path(os.path.realpath(p)) for p in config.allowed_paths]
        return search_root, allowed, None

    @staticmethod
    def _rglob_safe(
        root: Path,
        pattern: str,
        allowed: list[Path],
        limit: int = 500,
    ) -> list[Path]:
        """rglob that rejects files resolving outside allowed_paths.

        Uses itertools.islice on the raw generator BEFORE sorting to avoid
        materialising the full file list into memory (fixes the OOM risk
        present when sorted() wraps an unbounded rglob generator directly).
        OSError on dangling symlinks is silently skipped.

        NOTE: Python < 3.13 rglob follows symlinks during traversal.
        The islice(limit * 4) cap bounds memory exposure to 4× limit entries
        before the allowed-paths filter runs. Upgrade path: pass
        follow_symlinks=False to rglob when Python 3.13 is baseline.
        """
        # islice at 4× limit so we have headroom after the allowed-path filter
        # without materialising the full workspace.
        candidates = itertools.islice(root.rglob(pattern), limit * 4)
        results: list[Path] = []
        for f in candidates:
            if len(results) >= limit:
                break
            try:
                resolved = f.resolve()
            except OSError:
                log.debug("_rglob_safe: skipping unresolvable path %s", f)
                continue
            if any(resolved == a or resolved.is_relative_to(a) for a in allowed):
                results.append(f)
            else:
                log.debug(
                    "_rglob_safe: skipping %s — resolves outside allowed_paths",
                    f,
                )
        return results

    @staticmethod
    def _safe_json(obj: object, max_bytes: int = 24_000) -> str:
        """Serialize *obj* to JSON, trimming list fields if the result
        exceeds *max_bytes*.

        Produces valid JSON with a top-level 'truncated: true' flag rather
        than byte-slicing the serialized string (which produces invalid JSON).
        Iterates at most 20 times; if still over budget, returns a minimal
        error envelope. The 20-iteration cap is safe because each pass
        reduces the largest list by half.
        """
        raw = json.dumps(obj)
        if len(raw) <= max_bytes:
            return raw

        # Work on a shallow copy to avoid mutating the caller's data
        work: dict = dict(obj) if isinstance(obj, dict) else {"data": obj}
        work["truncated"] = True

        for _ in range(20):
            raw = json.dumps(work)
            if len(raw) <= max_bytes:
                return raw
            # Trim the longest list-valued key by half
            list_keys = [k for k, v in work.items() if isinstance(v, list)]
            if not list_keys:
                break
            biggest_key = max(list_keys, key=lambda k: len(work[k]))
            current_len = len(work[biggest_key])
            new_len = max(1, current_len // 2)
            if new_len == current_len:
                break  # Can't shrink further; bail to error envelope
            work[biggest_key] = work[biggest_key][:new_len]

        # Final fallback: minimal error envelope always fits
        return json.dumps({"error": "output too large to serialize", "truncated": True})


def enforce_atomic_validation(tool_cls):
    """Class decorator that ensures tools with requires_path_check=True use atomic validation.
    
    This decorator checks at class definition time and injects runtime assertions
    to ensure _validate_path_atomic is called in the execute method.
    """
    if not hasattr(tool_cls, 'requires_path_check') or not tool_cls.requires_path_check:
        return tool_cls
    
    # Store the original execute method
    original_execute = tool_cls.execute
    
    # Create a wrapper that checks for atomic validation
    async def execute_wrapper(self, config, **kwargs):
        # Check if the tool is using the new atomic validation
        # We'll add a flag to track validation usage
        self._atomic_validation_used = False
        
        # Create a wrapper for _validate_atomic_path to track usage
        original_validate = self._validate_atomic_path
        async def tracked_validate(*args, **kwargs):
            self._atomic_validation_used = True
            return await original_validate(*args, **kwargs)
        self._validate_atomic_path = tracked_validate
        
        # Also track usage of the new _validate_path_atomic method
        original_validate_path = self._validate_path_atomic
        async def tracked_validate_path(*args, **kwargs):
            self._atomic_validation_used = True
            return await original_validate_path(*args, **kwargs)
        self._validate_path_atomic = tracked_validate_path
        
        try:
            result = await original_execute(self, config, **kwargs)
            
            # Log warning if atomic validation wasn't used
            if not getattr(self, '_atomic_validation_used', False):
                import logging
                logging.warning(
                    f"Tool {tool_cls.name} with requires_path_check=True "
                    f"may not be using atomic path validation"
                )
            
            return result
        finally:
            # Restore original methods
            self._validate_atomic_path = original_validate
            self._validate_path_atomic = original_validate_path
    
    # Replace the execute method
    tool_cls.execute = execute_wrapper
    
    # Add a class attribute to mark it as decorated
    tool_cls._enforces_atomic_validation = True
    
    return tool_cls
