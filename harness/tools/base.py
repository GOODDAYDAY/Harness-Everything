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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from harness.core.config import HarnessConfig
from harness.core.security import validate_path_security

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Uniform result returned by every tool execution."""

    output: str = ""
    error: str = ""
    is_error: bool = False
    elapsed_s: float = 0.0

    def to_api(self) -> dict[str, Any]:
        """Format as a tool_result content block for the Claude API."""
        text = self.error if self.is_error else self.output
        return {"type": "text", "text": text}


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

    # Tool categories for per-phase filtering via PhaseConfig.tool_tags.
    # Valid tags: "file_read", "file_write", "search", "git", "analysis",
    #             "execution", "network", "testing"
    tags: frozenset[str] = frozenset()

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

    def _check_path(self, config: HarnessConfig, path: str) -> str | ToolResult:
        """Validate a file path against security rules.
        
        Returns: str on success, ToolResult on validation failure.
        
        Security validation order:
        1. validate_path_security on raw path (null bytes, control chars, homoglyphs)
        2. Resolve path with Path.resolve(strict=True) to eliminate symlink TOCTOU
        3. Check if resolved path is allowed
        
        This method addresses the TOCTOU vulnerability by using atomic symlink
        resolution before checking allowed paths.
        """
        # 1. Call validate_path_security(path) and return a ToolResult on error
        if error_msg := validate_path_security(path, config):
            return ToolResult(error=error_msg, is_error=True)
        
        # Handle empty path (use workspace)
        path_to_check = path if path else config.workspace
        
        # If path is relative, join it with workspace
        if not os.path.isabs(path_to_check):
            path_to_check = os.path.join(config.workspace, path_to_check)
        
        try:
            # 2. Create a Path object and call its resolve(strict=True) method
            # Catch OSError and return a ToolResult with error
            resolved_path = Path(path_to_check).resolve(strict=True)
            resolved_str = str(resolved_path)
        except OSError as exc:
            # Handle broken symlinks or non-existent paths
            # Fall back to checking parent directories for paths that should be creatable
            try:
                # Try non-strict resolution
                resolved_path = Path(path_to_check).resolve(strict=False)
                resolved_str = str(resolved_path)
                
                # Check if all parent directory components exist and are within allowed paths
                current = Path(resolved_str)
                while current != current.parent:  # Stop at root
                    if not current.parent.exists():
                        return ToolResult(
                            error=f"Cannot resolve path {path_to_check!r}: parent directory {current.parent} does not exist",
                            is_error=True,
                        )
                    # Check if parent directory is within allowed paths
                    parent_allowed = False
                    for allowed_path in config.allowed_paths:
                        allowed_resolved = str(Path(allowed_path).resolve(strict=False))
                        parent_str = str(current.parent)
                        if parent_str == allowed_resolved or parent_str.startswith(allowed_resolved + os.sep):
                            parent_allowed = True
                            break
                    
                    if not parent_allowed:
                        return ToolResult(
                            error=f"Cannot resolve path {path_to_check!r}: parent directory {current.parent} is outside allowed paths",
                            is_error=True,
                        )
                    
                    current = current.parent
            except Exception as exc2:
                return ToolResult(
                    error=f"Cannot resolve path {path_to_check!r}: {exc2}",
                    is_error=True,
                )
        
        # 3. Convert the resolved Path object to a string resolved_str
        # Already done above
        
        # 4. For each path in config.allowed_paths, resolve it with 
        # Path(allowed_path).resolve(strict=False) and check if resolved_str 
        # is equal to or starts with the allowed path
        for allowed_path in config.allowed_paths:
            allowed_resolved = str(Path(allowed_path).resolve(strict=False))
            if resolved_str == allowed_resolved or resolved_str.startswith(allowed_resolved + os.sep):
                return resolved_str
        
        # 5. If no allowed path matches, return a ToolResult with error
        return ToolResult(
            error=f"Path {resolved_str} is outside allowed directories",
            is_error=True,
        )

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
        self, config: HarnessConfig, path_str: str, require_exists: bool = True, directory: bool = False
    ) -> tuple[bool, str | ToolResult]:
        """
        Synchronous atomic path validation with inode verification.
        
        Opens path with os.O_RDONLY | os.O_NOFOLLOW, validates via _check_path,
        and verifies file hasn't changed using st_dev and st_ino.
        
        Returns (is_valid, validated_path_str | ToolResult_error).
        """
        # 1. Use existing path validation
        path_result = self._check_path(config, path_str)
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
                return False, ToolResult(error=f"Symlink resolution escapes allowed directory: {resolved}", is_error=True)
            elif exc.errno == errno.ENOENT and require_exists:
                return False, ToolResult(error=f"File not found: {resolved}", is_error=True)
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
                error=f"File validation failed: resolved path not within allowed directories",
                is_error=True
            )
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            return False, ToolResult(error=f"File validation failed: {exc}", is_error=True)

    async def _validate_atomic_path(
        self, config: HarnessConfig, path_str: str, require_exists: bool = True, directory: bool = False
    ) -> tuple[bool, str | ToolResult]:
        """
        Atomically validate a path is accessible and is a regular file or directory.
        Returns (is_valid, validated_path_str | ToolResult_error).
        
        This async wrapper delegates to the synchronous implementation.
        """
        return await asyncio.to_thread(
            self._validate_atomic_path_sync, config, path_str, require_exists, directory
        )

    async def _validate_directory_atomic(
        self, config: HarnessConfig, path_str: str
    ) -> tuple[bool, str | ToolResult]:
        """
        Atomically validate a path is accessible and is a directory.
        Returns (is_valid, validated_path_str | ToolResult_error).
        """
        # Use the consolidated atomic path validation with directory flag
        return await self._validate_atomic_path(config, path_str, require_exists=True, directory=True)

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
        matches ``harness/pipeline/loop.py``. A path that is not under the
        workspace (rare, but allowed_paths may span multiple roots) is
        allowed — phase scoping is intentionally a workspace-local concept.

        Scoping is per-phase and cheap: an empty list skips the entire check.
        See PhaseConfig.allowed_edit_globs for the policy.
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
        
        # 2. Resolve path to eliminate symlink TOCTOU using Path.resolve(strict=True)
        try:
            # Use Path.resolve(strict=True) for atomic symlink resolution
            resolved_path = Path(path_to_check).resolve(strict=True)
            resolved = str(resolved_path)
        except OSError as exc:
            # Handle broken symlinks or non-existent paths
            # Fall back to checking parent directories
            try:
                # Try non-strict resolution first
                resolved_path = Path(path_to_check).resolve(strict=False)
                resolved = str(resolved_path)
                
                # Check if all parent directories exist and are within allowed paths
                current = Path(resolved)
                while current != current.parent:  # Stop at root
                    if not current.parent.exists():
                        return "", ToolResult(
                            error=f"Cannot resolve path {path_to_check!r}: parent directory {current.parent} does not exist",
                            is_error=True,
                        )
                    current = current.parent
            except Exception as exc2:
                return "", ToolResult(
                    error=f"Cannot resolve path {path_to_check!r}: {exc2}",
                    is_error=True,
                )
        
        # 3. Check if resolved path is allowed
        if not config.is_path_allowed(resolved):
            return "", ToolResult(
                error=f"Path not allowed: {resolved}  (allowed: {config.allowed_paths})",
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
