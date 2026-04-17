"""Abstract base for all tools."""

from __future__ import annotations

import itertools
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.core.security import validate_path_no_homoglyphs

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

    def _check_path(self, config: HarnessConfig, path: str) -> ToolResult | None:
        """Return a ToolResult error if *path* is outside allowed dirs, else None.

        Rejects null bytes before any Path operation — a null byte in a path
        string causes undefined behaviour on some OSes and can be used to
        truncate the path at the OS level, bypassing prefix checks.
        """
        # Check for Unicode homoglyphs FIRST (security ordering fix)
        if error_msg := validate_path_no_homoglyphs(path):
            return ToolResult(
                error=f"PERMISSION ERROR: {error_msg}",
                is_error=True,
            )
        
        if "\x00" in path:
            return ToolResult(
                error=f"PERMISSION ERROR: path contains null byte: {path!r}",
                is_error=True,
            )
        
        # Check for control characters \x01 through \x1f (except \t, \n, \r)
        for i in range(1, 0x20):
            if i in (0x09, 0x0A, 0x0D):  # \t, \n, \r are allowed
                continue
            if chr(i) in path:
                return ToolResult(
                    error=f"PERMISSION ERROR: path contains control character {chr(i)!r} (\\x{i:02x}): {path!r}",
                    is_error=True,
                )
        
        if not config.is_path_allowed(path):
            return ToolResult(
                error=f"Path not allowed: {path}  (allowed: {config.allowed_paths})",
                is_error=True,
            )
        return None



    def _resolve_and_check(
        self, config: HarnessConfig, path: str
    ) -> tuple[str, ToolResult | None]:
        """Null-byte check → resolve → allowed-paths check in one call.

        Replaces the scattered ``resolved = str(Path(path).resolve())`` +
        ``self._check_path(config, resolved)`` pattern in every file tool.
        Performing the null-byte check *before* ``Path(path)`` is constructed
        closes a subtle gap: CPython raises ``ValueError`` on a null byte inside
        ``Path()``, which surfaces as an opaque TOOL ERROR from the registry
        rather than the expected PERMISSION ERROR.

        Returns:
            (resolved_path, None) on success.
            ("", error_ToolResult) on any failure.
        """
        if "\x00" in path:
            return "", ToolResult(
                error=f"PERMISSION ERROR: path contains null byte: {path!r}",
                is_error=True,
            )
        
        # Check for control characters \x01 through \x1f (except \t, \n, \r)
        for i in range(1, 0x20):
            if i in (0x09, 0x0A, 0x0D):  # \t, \n, \r are allowed
                continue
            if chr(i) in path:
                return "", ToolResult(
                    error=f"PERMISSION ERROR: path contains control character {chr(i)!r} (\\x{i:02x}): {path!r}",
                    is_error=True,
                )
        
        # Check for Unicode homoglyphs that could bypass security
        if self.requires_path_check:
            if error_msg := validate_path_no_homoglyphs(path, config):
                return "", ToolResult(error=error_msg, is_error=True)
        
        try:
            resolved = str(Path(os.path.realpath(path)))
        except (ValueError, OSError) as exc:
            return "", ToolResult(
                error=f"PERMISSION ERROR: invalid path {path!r}: {exc}",
                is_error=True,
            )
        if not config.is_path_allowed(resolved):
            return "", ToolResult(
                error=f"Path not allowed: {resolved}  (allowed: {config.allowed_paths})",
                is_error=True,
            )
        return resolved, None

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

        Guards:
        - Null-byte rejection before any Path operation.
        - 'PERMISSION ERROR' prefix so HarnessLoop's error-category logic fires.
        """
        if "\x00" in root:
            return Path("."), [], ToolResult(
                error=f"PERMISSION ERROR: root contains null byte: {root!r}",
                is_error=True,
            )
        # Use os.path.realpath (calls realpath(3)) for symlink resolution —
        # consistent with HarnessConfig.is_path_allowed() and immune to the
        # Path.resolve() differences on Python < 3.6 edge cases.
        raw_root = root if root else config.workspace
        search_root = Path(os.path.realpath(raw_root))
        allowed = [Path(os.path.realpath(p)) for p in config.allowed_paths]
        if not any(
            search_root == a or search_root.is_relative_to(a) for a in allowed
        ):
            return search_root, [], ToolResult(
                error=(
                    f"PERMISSION ERROR: root {str(search_root)!r} is outside "
                    f"allowed_paths {config.allowed_paths}"
                ),
                is_error=True,
            )
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
