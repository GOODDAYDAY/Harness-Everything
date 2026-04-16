"""file_patch — apply a unified diff (patch) to files in the workspace.

Applies one or more hunks from a standard ``diff -u`` / ``git diff`` patch to
real files without shelling out to ``patch(1)``.  Pure stdlib — no extra
dependencies.

Supported input forms
---------------------
* A **multi-file patch** produced by ``git diff`` or ``diff -u -r``:
  each ``--- a/... +++ b/...`` header starts a new file section.
* A **single-file patch** where ``path`` is supplied explicitly and the patch
  text starts directly with ``@@ -... @@`` (no ``---``/``+++`` headers needed).

Hunk application
----------------
Each ``@@`` hunk is applied independently:

1. The original context + removed lines are located in the target file starting
   from the hunk's declared line number (with a configurable fuzz tolerance for
   line-offset drift).
2. If the exact match fails at the declared offset, the algorithm searches
   ±``fuzz`` lines around it before giving up.
3. Applied hunks are accumulated; once every hunk for a file has been
   processed the result is written atomically (all-or-nothing per file).

Dry-run mode
------------
Pass ``dry_run=True`` to preview what *would* change without touching the
filesystem.  The output shows each hunk's status and the full resulting file
content for single-file patches.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# ---------------------------------------------------------------------------
# Internal patch data structures
# ---------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


class _Hunk:
    """One ``@@`` block from a unified diff."""

    __slots__ = (
        "old_start",  # 1-based line in original
        "old_count",  # lines consumed from original (0 = insertion-only)
        "new_start",  # 1-based line in patched result
        "new_count",  # lines produced in result
        "lines",      # raw diff lines ('+', '-', ' ' prefixed)
    )

    def __init__(
        self,
        old_start: int,
        old_count: int,
        new_start: int,
        new_count: int,
        lines: list[str],
    ) -> None:
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines = lines

    @property
    def context_before(self) -> list[str]:
        """Leading context lines (stripped of the ' ' prefix)."""
        result: list[str] = []
        for ln in self.lines:
            if ln.startswith(" "):
                result.append(ln[1:])
            else:
                break
        return result

    @property
    def old_lines(self) -> list[str]:
        """Lines present in the *original* (context + removed)."""
        return [ln[1:] for ln in self.lines if ln.startswith((" ", "-"))]

    @property
    def new_lines(self) -> list[str]:
        """Lines present in the *patched* result (context + added)."""
        return [ln[1:] for ln in self.lines if ln.startswith((" ", "+"))]


# ---------------------------------------------------------------------------
# Parser: text → list[_Hunk]
# ---------------------------------------------------------------------------


def _parse_hunks(patch_text: str) -> list[_Hunk]:
    """Parse all ``@@`` hunks from *patch_text*.  File headers are ignored."""
    hunks: list[_Hunk] = []
    lines = patch_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        m = _HUNK_HEADER_RE.match(lines[i])
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            hunk_lines: list[str] = []
            while i < len(lines) and not _HUNK_HEADER_RE.match(lines[i]):
                raw = lines[i]
                # Stop at next file header in a multi-file patch
                if raw.startswith(("--- ", "+++ ", "diff --git ")):
                    break
                # Strip trailing newline for uniform handling; keep prefix char
                stripped = raw.rstrip("\n\r")
                if stripped and stripped[0] in (" ", "+", "-"):
                    hunk_lines.append(stripped)
                elif stripped.startswith("\\"):
                    # "\ No newline at end of file" — skip marker
                    pass
                i += 1
            hunks.append(
                _Hunk(old_start, old_count, new_start, new_count, hunk_lines)
            )
        else:
            i += 1
    return hunks


# ---------------------------------------------------------------------------
# Parser: multi-file patch → {path: patch_text}
# ---------------------------------------------------------------------------

_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/.+ b/(.+)$")


def _split_by_file(patch_text: str) -> dict[str, str]:
    """Split a multi-file unified diff into per-file patch text chunks.

    Keys are the target file paths extracted from ``+++ b/<path>`` headers.
    Returns an empty dict if no ``+++`` / ``diff --git`` headers are found,
    indicating a single-file patch.
    """
    result: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    for line in patch_text.splitlines(keepends=True):
        # git diff header
        m = _DIFF_GIT_RE.match(line)
        if m:
            if current_path and current_lines:
                result[current_path] = "".join(current_lines)
            current_path = m.group(1).strip()
            current_lines = []
            continue
        # +++ header
        m = _FILE_HEADER_RE.match(line.rstrip("\n\r"))
        if m:
            if current_path is not None and current_lines:
                result[current_path] = "".join(current_lines)
            current_path = m.group(1).strip()
            current_lines = []
            continue
        if current_path is not None:
            current_lines.append(line)

    if current_path and current_lines:
        result[current_path] = "".join(current_lines)

    return result


# ---------------------------------------------------------------------------
# Hunk application engine
# ---------------------------------------------------------------------------


class _PatchError(Exception):
    """Raised when a hunk cannot be applied."""


def _apply_hunk(file_lines: list[str], hunk: _Hunk, fuzz: int) -> list[str]:
    """Apply *hunk* to *file_lines* and return the updated line list.

    Args:
        file_lines: Current file content as a list of lines **without** trailing
                    newlines (i.e. split by ``splitlines()``).
        hunk:       The hunk to apply.
        fuzz:       Maximum line-offset drift tolerated when the declared start
                    line does not match.

    Raises:
        _PatchError: When the hunk's old lines cannot be located in the file.
    """
    old = hunk.old_lines
    new = hunk.new_lines

    # Declared 0-based start index (unified diff lines are 1-based)
    declared = max(0, hunk.old_start - 1)

    # Special case: pure insertion hunk (old_count == 0) — just splice in
    if hunk.old_count == 0:
        insert_at = declared
        return file_lines[:insert_at] + new + file_lines[insert_at:]

    # Try to locate the old block within the fuzz window
    match_at: int | None = None
    for delta in range(fuzz + 1):
        for sign in ([0] if delta == 0 else [delta, -delta]):
            candidate = declared + sign
            if candidate < 0 or candidate + len(old) > len(file_lines):
                continue
            if file_lines[candidate : candidate + len(old)] == old:
                match_at = candidate
                break
        if match_at is not None:
            break

    if match_at is None:
        # Build a readable excerpt for the error message
        excerpt = "\n".join(f"  {ln!r}" for ln in old[:6])
        raise _PatchError(
            f"Hunk @@ -{hunk.old_start},{hunk.old_count} could not be located "
            f"(fuzz={fuzz}).  Expected lines:\n{excerpt}"
        )

    return file_lines[:match_at] + new + file_lines[match_at + len(old):]


def _apply_hunks_to_text(
    original: str, hunks: list[_Hunk], fuzz: int
) -> tuple[str, list[str]]:
    """Apply all *hunks* to *original* text and return ``(patched_text, applied_labels)``.

    Hunks are applied in declaration order.  Because each successful hunk
    shifts subsequent line numbers, we track the cumulative offset.

    Returns:
        patched_text:   The modified file content.
        applied_labels: Human-readable description of each applied hunk.
    """
    # Preserve trailing newline convention of the original
    ends_with_newline = original.endswith("\n")
    lines = original.splitlines()

    applied: list[str] = []
    offset = 0  # cumulative line-count delta from previously applied hunks

    # Sort by old_start so we can apply in file order regardless of patch order
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        # Adjust declared start by accumulated offset
        adjusted = _Hunk(
            old_start=hunk.old_start + offset,
            old_count=hunk.old_count,
            new_start=hunk.new_start,
            new_count=hunk.new_count,
            lines=hunk.lines,
        )
        lines = _apply_hunk(lines, adjusted, fuzz)
        delta = hunk.new_count - hunk.old_count
        offset += delta
        applied.append(
            f"  @@ -{hunk.old_start},{hunk.old_count} "
            f"+{hunk.new_start},{hunk.new_count} @@ applied"
        )

    result = "\n".join(lines)
    if ends_with_newline and not result.endswith("\n"):
        result += "\n"
    return result, applied


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class FilePatchTool(Tool):
    """Apply a unified diff (patch) to one or more files in the workspace.

    Accepts standard ``diff -u`` / ``git diff`` output.  Each ``@@`` hunk is
    located in the target file using a configurable fuzz tolerance for
    line-offset drift, then the file is rewritten atomically.

    Multi-file patches (with ``--- a/...`` / ``+++ b/...`` headers) are split
    and each file is patched independently.  For single-file patches without
    headers, supply the explicit target ``path``.

    Use ``dry_run=true`` to preview the result without writing any files.
    """

    name = "file_patch"
    description = (
        "Apply a unified diff (patch) to files in the workspace. "
        "Accepts standard 'diff -u' or 'git diff' output. "
        "For multi-file patches the target paths are read from the '+++ b/...' headers. "
        "For single-hunk patches without headers, supply 'path' explicitly. "
        "Use dry_run=true to preview changes without writing."
    )
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Unified diff text. May be a full multi-file patch "
                        "(with '--- a/...' / '+++ b/...' headers) or a bare "
                        "hunk sequence starting with '@@ -... @@'."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Target file path. Required when the patch contains no "
                        "'+++ b/...' headers. Ignored when headers are present."
                    ),
                    "default": "",
                },
                "fuzz": {
                    "type": "integer",
                    "description": (
                        "Line-offset fuzz tolerance: how many lines the hunk may "
                        "drift from its declared position (default: 3)."
                    ),
                    "default": 3,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without writing (default: false).",
                    "default": False,
                },
            },
            "required": ["patch"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        patch: str,
        path: str = "",
        fuzz: int = 3,
        dry_run: bool = False,
    ) -> ToolResult:
        if not patch.strip():
            return ToolResult(error="patch text is empty", is_error=True)

        # ------------------------------------------------------------------
        # Decide: multi-file patch or single-file patch?
        # ------------------------------------------------------------------
        file_patches = _split_by_file(patch)

        if file_patches:
            # Multi-file: apply each independently
            return await self._apply_multi(config, file_patches, fuzz, dry_run)

        # Single-file: path must be supplied
        if not path:
            return ToolResult(
                error=(
                    "patch contains no '+++ b/...' file headers — "
                    "supply 'path' to identify the target file"
                ),
                is_error=True,
            )
        return await self._apply_single(config, path, patch, fuzz, dry_run)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _apply_single(
        self,
        config: HarnessConfig,
        path: str,
        patch_text: str,
        fuzz: int,
        dry_run: bool,
    ) -> ToolResult:
        """Parse and apply all hunks from *patch_text* to the file at *path*."""
        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err

        p = Path(resolved)

        # Support patching a non-existent file when the patch is a pure creation
        # (all hunks have old_count == 0 and old_start == 0).
        if p.exists():
            if not p.is_file():
                return ToolResult(
                    error=f"Not a file: {resolved}", is_error=True
                )
            original = p.read_text(encoding="utf-8", errors="replace")
        else:
            original = ""

        hunks = _parse_hunks(patch_text)
        if not hunks:
            return ToolResult(
                error=f"No valid '@@' hunks found in patch for {path}",
                is_error=True,
            )

        try:
            patched, applied_labels = _apply_hunks_to_text(original, hunks, fuzz)
        except _PatchError as exc:
            return ToolResult(error=str(exc), is_error=True)

        lines_before = original.count("\n") + (1 if original else 0)
        lines_after = patched.count("\n") + (1 if patched else 0)
        delta_str = (
            f"+{lines_after - lines_before}"
            if lines_after >= lines_before
            else str(lines_after - lines_before)
        )

        summary_lines = [
            f"{'[DRY RUN] ' if dry_run else ''}Patched {path}",
            f"  Hunks applied: {len(applied_labels)}",
            *applied_labels,
            f"  Lines: {lines_before} → {lines_after} ({delta_str})",
        ]

        if dry_run:
            summary_lines.append("\n--- Resulting file content ---")
            summary_lines.append(patched)
            return ToolResult(output="\n".join(summary_lines))

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(patched, encoding="utf-8")
        except OSError as exc:
            return ToolResult(error=f"Write failed: {exc}", is_error=True)

        return ToolResult(output="\n".join(summary_lines))

    async def _apply_multi(
        self,
        config: HarnessConfig,
        file_patches: dict[str, str],
        fuzz: int,
        dry_run: bool,
    ) -> ToolResult:
        """Apply each file's patch chunk independently; report aggregate results."""
        output_parts: list[str] = []
        errors: list[str] = []
        files_written: list[str] = []

        for rel_path, patch_text in file_patches.items():
            # Resolve relative to workspace; _resolve_and_check handles null-byte
            # rejection and realpath-based symlink resolution.
            raw = str(Path(config.workspace) / rel_path)
            resolved, chk_err = self._resolve_and_check(config, raw)
            if chk_err:
                errors.append(f"{rel_path}: {chk_err.error}")
                continue

            p = Path(resolved)
            original = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

            hunks = _parse_hunks(patch_text)
            if not hunks:
                errors.append(f"{rel_path}: no valid hunks found")
                continue

            try:
                patched, applied_labels = _apply_hunks_to_text(original, hunks, fuzz)
            except _PatchError as exc:
                errors.append(f"{rel_path}: {exc}")
                continue

            lines_before = original.count("\n") + (1 if original else 0)
            lines_after = patched.count("\n") + (1 if patched else 0)
            delta_str = (
                f"+{lines_after - lines_before}"
                if lines_after >= lines_before
                else str(lines_after - lines_before)
            )

            file_summary = [
                f"{'[DRY RUN] ' if dry_run else ''}Patched {rel_path}",
                f"  Hunks: {len(applied_labels)}  Lines: {lines_before}→{lines_after} ({delta_str})",
            ]
            output_parts.extend(file_summary)

            if not dry_run:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(patched, encoding="utf-8")
                    files_written.append(rel_path)
                except OSError as exc:
                    errors.append(f"{rel_path}: write failed: {exc}")

        # Build final output
        result_lines: list[str] = output_parts[:]
        if errors:
            result_lines.append("\nErrors:")
            result_lines.extend(f"  {e}" for e in errors)
        if files_written:
            result_lines.append(
                f"\n{len(files_written)} file(s) written: {', '.join(files_written)}"
            )

        if errors and not output_parts:
            return ToolResult(
                error="\n".join(result_lines),
                is_error=True,
            )
        return ToolResult(output="\n".join(result_lines))
