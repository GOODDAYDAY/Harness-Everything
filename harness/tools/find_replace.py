"""find_replace — multi-file regex search-and-replace across the workspace.

Unlike ``edit_file`` (which operates on exactly one file at a time),
``find_replace`` applies a single regex substitution across every file that
matches a glob pattern.  This makes it the right tool for:

* Symbol renames — rename a class, function, or variable everywhere it appears
  in a single operation rather than dozens of sequential ``edit_file`` calls.
* Import path updates — when a module is moved, update every ``import`` or
  ``from … import`` statement in one shot.
* Bulk string fixes — correct a repeated typo, outdated constant, or changed
  API name across many files.

Key design decisions
--------------------
* **Regex-based** — the ``pattern`` argument is a Python ``re`` pattern, which
  handles word-boundary anchors (``\\bOldName\\b``), capture groups and
  back-references in ``replacement`` (``\\1``), and multi-line patterns with
  ``re.MULTILINE``.
* **Exact-literal shortcut** — when ``literal=true`` the pattern is treated as
  a plain string (``re.escape``-d internally) so callers don't need to escape
  metacharacters for simple word replacements.
* **Scope control** — ``file_glob`` (default ``**/*.py``) and ``path`` (default
  workspace root) narrow the search space so a rename that only affects Python
  files never accidentally clobbers a binary or a lockfile.
* **Dry-run mode** — ``dry_run=true`` reports what *would* change (per-file
  match counts, first few match lines) without writing anything, so the caller
  can verify the scope of a rename before committing.
* **Safety guard** — ``max_files_changed`` (default 50) caps the number of
  files that can be rewritten in one call.  Callers must explicitly raise the
  cap for truly sweeping renames to prevent accidental mass-rewriting from a
  badly anchored pattern.
* **Path-checked** — every candidate file is validated against
  ``config.allowed_paths`` before it is read or written.
* **Atomic per-file writes** — each file is written in a single ``write_text``
  call; partial failures (one file unwritable) do not corrupt other files.

Output format
-------------
Text output (one line per file) shows the number of substitutions made::

    3 file(s) changed  (8 substitution(s) total)

    harness/llm.py                    2 substitution(s)
    harness/tools/registry.py         5 substitution(s)
    harness/config.py                 1 substitution(s)

In dry-run mode each file also shows the first matching line to confirm the
pattern is anchored correctly.

Comparison with existing tools
-------------------------------
| Tool             | Scope   | Match style  | Writes files? |
|------------------|---------|--------------|---------------|
| ``edit_file``    | 1 file  | plain string | yes           |
| ``grep_search``  | N files | regex (read) | no            |
| ``file_patch``   | N files | unified diff | yes           |
| ``find_replace`` | N files | regex        | yes           |

``find_replace`` fills the gap between ``grep_search`` (read-only, regex) and
``edit_file`` (write, single file).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_FILE_GLOB: str = "**/*.py"
_DEFAULT_MAX_FILES: int = 50        # safety cap — raise explicitly for large renames
_MAX_PREVIEW_LINES: int = 3         # dry-run: max matching lines shown per file
_MAX_OUTPUT_LINES: int = 200        # cap total output lines to avoid flooding context


# ---------------------------------------------------------------------------
# Core engine (pure, no I/O — easy to unit-test)
# ---------------------------------------------------------------------------


def _preview_matches(
    text: str,
    compiled: "re.Pattern[str]",
    max_lines: int,
) -> list[str]:
    """Return up to *max_lines* matching line excerpts for dry-run preview.

    Each entry is ``"    L{lineno}: {line}"`` (1-based, stripped).
    """
    previews: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        if compiled.search(line):
            stripped = line.strip()
            if len(stripped) > 120:
                stripped = stripped[:117] + "\u2026"
            previews.append(f"    L{i}: {stripped}")
            if len(previews) >= max_lines:
                break
    return previews


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class FindReplaceTool(Tool):
    """Regex search-and-replace across multiple files in the workspace.

    Applies a single regex substitution to every file matching a glob pattern,
    making it efficient for:

    * **Symbol renames** across the codebase (one call instead of N
      ``edit_file`` calls)
    * **Import path updates** when a module is moved or renamed
    * **Bulk string corrections** — typos, deprecated API names, changed
      constant values

    For simple literal replacements (no regex metacharacters needed), set
    ``literal=true`` to skip escaping.  For precise renames that must not
    touch substrings, use word-boundary anchors in the pattern:
    ``pattern="\\bOldClass\\b"``.

    Safety
    ------
    * ``dry_run=true`` (recommended before large renames) shows what *would*
      change — per-file match counts and first matching lines — without
      writing anything.
    * ``max_files_changed`` (default 50) hard-caps how many files can be
      rewritten in one call.  Raise it explicitly for genuinely large renames.
    * ``count`` limits substitutions per file (0 = unlimited, 1 = first only).
    """

    name = "find_replace"
    description = (
        "Regex search-and-replace across multiple files in the workspace. "
        "Replaces all occurrences of 'pattern' with 'replacement' in every file "
        "matching 'file_glob' (default: **/*.py) under 'path' (default: workspace). "
        "Use literal=true for plain-string replacement (no regex escaping needed). "
        "Use dry_run=true to preview changes without writing. "
        "Efficient for symbol renames, import path updates, and bulk fixes. "
        "Safety cap: max_files_changed (default 50) limits blast radius."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Python regex pattern to search for. "
                        "Use '\\\\bName\\\\b' for whole-word matching. "
                        "Supports capture groups and back-references in replacement. "
                        "Set literal=true to treat this as a plain string instead."
                    ),
                },
                "replacement": {
                    "type": "string",
                    "description": (
                        "Replacement string. Back-references (\\\\1, \\\\g<name>) "
                        "refer to capture groups in pattern when literal=false. "
                        "Use an empty string to delete all matches."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Root directory to search (default: workspace root). "
                        "Relative to the workspace."
                    ),
                    "default": "",
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern selecting which files to search "
                        "(default: '**/*.py'). "
                        "E.g. '**/*.py', 'src/**/*.ts', '*.md'."
                    ),
                    "default": _DEFAULT_FILE_GLOB,
                },
                "literal": {
                    "type": "boolean",
                    "description": (
                        "Treat pattern as a plain string rather than a regex "
                        "(default: false). Automatically escapes metacharacters."
                    ),
                    "default": False,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive matching (default: false).",
                    "default": False,
                },
                "multiline": {
                    "type": "boolean",
                    "description": (
                        "Enable re.MULTILINE mode so '^' and '$' match line "
                        "boundaries (default: false)."
                    ),
                    "default": False,
                },
                "count": {
                    "type": "integer",
                    "description": (
                        "Maximum substitutions per file "
                        "(0 = unlimited, default: 0). "
                        "Set to 1 to replace only the first occurrence per file."
                    ),
                    "default": 0,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Preview changes without writing any files (default: false). "
                        "Strongly recommended before large renames."
                    ),
                    "default": False,
                },
                "max_files_changed": {
                    "type": "integer",
                    "description": (
                        "Hard cap on files rewritten in one call (default: 50). "
                        "Raise explicitly for large-scale renames."
                    ),
                    "default": _DEFAULT_MAX_FILES,
                },
            },
            "required": ["pattern", "replacement"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        pattern: str,
        replacement: str,
        path: str = "",
        file_glob: str = _DEFAULT_FILE_GLOB,
        literal: bool = False,
        case_insensitive: bool = False,
        multiline: bool = False,
        count: int = 0,
        dry_run: bool = False,
        max_files_changed: int = _DEFAULT_MAX_FILES,
    ) -> ToolResult:
        # ------------------------------------------------------------------ #
        # 1. Validate inputs
        # ------------------------------------------------------------------ #
        if not pattern:
            return ToolResult(error="pattern must not be empty", is_error=True)

        re_flags = 0
        if case_insensitive:
            re_flags |= re.IGNORECASE
        if multiline:
            re_flags |= re.MULTILINE

        effective_pattern = re.escape(pattern) if literal else pattern
        try:
            compiled = re.compile(effective_pattern, re_flags)
        except re.error as exc:
            return ToolResult(
                error=f"Invalid regex pattern {pattern!r}: {exc}",
                is_error=True,
            )

        if count < 0:
            return ToolResult(
                error=f"count must be >= 0 (got {count})", is_error=True
            )
        if max_files_changed < 1:
            return ToolResult(
                error=f"max_files_changed must be >= 1 (got {max_files_changed})",
                is_error=True,
            )

        # ------------------------------------------------------------------ #
        # 2. Resolve root directory
        # ------------------------------------------------------------------ #
        raw_root = str(Path(config.workspace) / path) if path else config.workspace
        root_str, err = self._resolve_and_check(config, raw_root)
        if err:
            return err
        root = Path(root_str)

        if not root.is_dir():
            return ToolResult(
                error=f"Search root is not a directory: {root_str}", is_error=True
            )

        # ------------------------------------------------------------------ #
        # 3. Collect candidate files
        # ------------------------------------------------------------------ #
        try:
            candidate_paths = sorted(
                (f for f in root.glob(file_glob) if f.is_file()),
                key=lambda f: str(f),  # deterministic order for reproducibility
            )
        except Exception as exc:
            return ToolResult(
                error=f"Failed to glob '{file_glob}' under {root_str}: {exc}",
                is_error=True,
            )

        if not candidate_paths:
            return ToolResult(
                output=(
                    f"No files match glob '{file_glob}' under {root_str}.\n"
                    "No changes made."
                )
            )

        # ------------------------------------------------------------------ #
        # 4. Scan files for matches
        # ------------------------------------------------------------------ #
        # Each entry: (rel_path, original_text, new_text, n_subs, preview_lines)
        pending: list[tuple[str, str, str, int, list[str]]] = []
        files_scanned = 0
        read_errors: list[str] = []

        for fpath in candidate_paths:
            # Per-file path check (catches symlinks pointing outside workspace)
            if self._check_path(config, str(fpath)) is not None:
                continue

            try:
                original = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                read_errors.append(f"  {fpath}: {exc}")
                continue

            files_scanned += 1

            # Fast reject: skip files with no match at all
            if not compiled.search(original):
                continue

            # Apply substitution
            if count == 0:
                new_text, n_subs = compiled.subn(replacement, original)
            else:
                new_text = compiled.sub(replacement, original, count=count)
                # Count actual substitutions made (capped at `count`)
                n_subs = min(count, len(compiled.findall(original)))

            if n_subs == 0:
                continue

            try:
                rel = str(fpath.relative_to(root))
            except ValueError:
                rel = str(fpath)

            previews = _preview_matches(original, compiled, _MAX_PREVIEW_LINES)
            pending.append((rel, original, new_text, n_subs, previews))

        # ------------------------------------------------------------------ #
        # 5. Enforce max_files_changed cap
        # ------------------------------------------------------------------ #
        over_cap = len(pending) > max_files_changed
        visible_pending = pending[:max_files_changed]

        # ------------------------------------------------------------------ #
        # 6. Write changes (unless dry_run)
        # ------------------------------------------------------------------ #
        write_errors: list[str] = []
        written: list[str] = []

        if not dry_run:
            for rel, _orig, new_text, _n, _prev in visible_pending:
                fpath = root / rel
                try:
                    fpath.write_text(new_text, encoding="utf-8")
                    written.append(rel)
                except OSError as exc:
                    write_errors.append(f"  {rel}: {exc}")

        # ------------------------------------------------------------------ #
        # 7. Build output
        # ------------------------------------------------------------------ #
        if not pending:
            output = (
                f"No matches found for pattern {pattern!r} "
                f"in {files_scanned} file(s) under {root_str}.\n"
                f"Glob: {file_glob}"
            )
            return ToolResult(output=output)

        total_subs = sum(n for _, _, _, n, _ in visible_pending)
        n_changed = len(visible_pending)

        output_lines: list[str] = []

        # Summary header
        if dry_run:
            output_lines.append(
                f"[DRY RUN]  {n_changed} file(s) would be changed  "
                f"({total_subs} substitution(s) total)"
            )
        else:
            actually_written = len(written)
            output_lines.append(
                f"{actually_written} file(s) changed  "
                f"({total_subs} substitution(s) total)"
            )

        mode_flags = []
        if literal:
            mode_flags.append("literal")
        else:
            mode_flags.append("regex")
        if case_insensitive:
            mode_flags.append("case-insensitive")
        if multiline:
            mode_flags.append("multiline")
        flags_str = ", ".join(mode_flags)

        output_lines.append(
            f"Pattern: {pattern!r}  \u2192  {replacement!r}  [{flags_str}]"
        )
        output_lines.append(
            f"Glob: {file_glob}  under: {root_str}  "
            f"(scanned {files_scanned} file(s))"
        )
        output_lines.append("")

        # Per-file detail
        name_width = max((len(rel) for rel, *_ in visible_pending), default=10)
        name_width = min(name_width, 60)

        for rel, _orig, _new, n_subs, previews in visible_pending:
            status = ""
            if not dry_run:
                status = "" if rel in written else "  [WRITE FAILED]"
            sub_word = "substitution" if n_subs == 1 else "substitutions"
            output_lines.append(
                f"  {rel:<{name_width}}  {n_subs} {sub_word}{status}"
            )
            if dry_run and previews:
                output_lines.extend(previews)

        # Cap guard warning
        if over_cap:
            skipped = len(pending) - max_files_changed
            output_lines.append(
                f"\n  \u26a0  {skipped} additional file(s) matched but were NOT "
                f"{'shown' if dry_run else 'changed'} — "
                f"max_files_changed={max_files_changed} cap reached.\n"
                f"  Increase max_files_changed or narrow file_glob / path to proceed."
            )

        # Error sections
        if write_errors:
            output_lines.append("\nWrite errors (these files were NOT updated):")
            output_lines.extend(write_errors)

        if read_errors:
            output_lines.append("\nRead errors (these files were skipped):")
            output_lines.extend(read_errors[:10])

        # Hard cap on total output length to prevent context flood
        if len(output_lines) > _MAX_OUTPUT_LINES:
            output_lines = output_lines[:_MAX_OUTPUT_LINES]
            output_lines.append(
                f"... [output truncated — more lines not shown]"
            )

        return ToolResult(output="\n".join(output_lines))
