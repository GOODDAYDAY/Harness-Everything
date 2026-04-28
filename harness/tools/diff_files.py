"""diff_files — show a unified diff between two files or between a file and a string.

Produces standard unified-diff output (the same format accepted by ``file_patch``)
using Python's stdlib ``difflib``.  No external tools or subprocesses are needed.

Use cases
---------
* **Before/after verification**: the executor reads a file before editing, then
  diffs the before-text against the current file to confirm exactly what changed.
* **Plan validation**: compare a "desired state" string against the current file
  to show the gap that remains, giving the evaluator precise evidence.
* **Regression detection**: diff two versions of the same file to spot accidental
  deletions or scope creep beyond the planned change.

Two operating modes
-------------------
* **file vs file** (``mode="file_vs_file"``): compare ``path_a`` against
  ``path_b``.  Both must be existing files inside allowed paths.
* **file vs text** (``mode="file_vs_text"``, default): compare the current
  content of ``path_a`` against the literal string ``text_b``.  This is the
  most common mode — the executor stores what it *wanted* the file to look like
  and diffs it against the actual current state.

Output
------
Standard unified-diff text with ``---``/``+++`` headers and ``@@`` hunk
markers.  When there are no differences the output says so explicitly (no
empty string that could be misread as an error).  Output is capped at
``max_lines`` lines (default 500) to prevent large files flooding the context.

Integration with ``file_patch``
--------------------------------
The output of this tool is valid input for ``file_patch`` — the executor can
use ``diff_files`` to see what needs to change, then ``file_patch`` to apply it.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# Default cap on output lines — prevents a diff of a large generated file from
# flooding the context window while still showing meaningful hunks.
_DEFAULT_MAX_LINES: int = 500
_DEFAULT_CONTEXT: int = 3   # unified diff context lines (standard is 3)


class DiffFilesTool(Tool):
    """Show a unified diff between two files or between a file and a text string.

    This is the read-only companion to ``file_patch``:

    * Use ``diff_files`` to *see* what changed (or what needs to change).
    * Use ``file_patch`` to *apply* the diff.

    Modes
    -----
    ``mode="file_vs_text"`` (default)
        Compare the current content of ``path_a`` against the literal string
        ``text_b``.  Useful for verifying that a file now matches an expected
        state, or for computing a patch from a desired state.

    ``mode="file_vs_file"``
        Compare two existing files.  Both must be inside allowed paths.

    Output
    ------
    Standard ``diff -u`` / ``git diff`` format.  The output is a valid patch
    that ``file_patch`` can apply.  Capped at ``max_lines`` lines (default 500)
    with a truncation notice when exceeded.

    Tip
    ---
    Set ``context=0`` to show only changed lines without surrounding context —
    useful when you just want to confirm a specific substitution was made.
    Set ``context=10`` or higher to see more of the surrounding code when
    reviewing a complex change.
    """

    name = "diff_files"
    description = (
        "Show a unified diff between two files, or between a file and a text string. "
        "mode='file_vs_text' (default): diff path_a against the literal text_b string. "
        "mode='file_vs_file': diff path_a against path_b (both must exist). "
        "Output is standard unified-diff format compatible with file_patch. "
        "Returns 'No differences' when the contents are identical. "
        "Capped at max_lines lines (default 500) to fit in context windows."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path_a": {
                    "type": "string",
                    "description": (
                        "Path to the first file (the 'before' / 'original' side). "
                        "Must exist and be inside allowed paths."
                    ),
                },
                "path_b": {
                    "type": "string",
                    "description": (
                        "Path to the second file (mode='file_vs_file' only). "
                        "Must exist and be inside allowed paths."
                    ),
                    "default": "",
                },
                "text_b": {
                    "type": "string",
                    "description": (
                        "Text to diff against path_a (mode='file_vs_text' only). "
                        "This is the 'after' / 'desired' side of the diff."
                    ),
                    "default": "",
                },
                "mode": {
                    "type": "string",
                    "enum": ["file_vs_text", "file_vs_file"],
                    "description": (
                        "'file_vs_text' (default): diff path_a vs literal text_b. "
                        "'file_vs_file': diff path_a vs path_b."
                    ),
                    "default": "file_vs_text",
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Required — no default. "
                        "Use 2-3 for local context, 5+ for broader view. "
                        "Set to 0 to show only changed lines."
                    ),
                },
                "max_lines": {
                    "type": "integer",
                    "description": (
                        "Required — no default. "
                        "Use 100-200 for quick diff, 500+ for full comparison. "
                        "Maximum output lines before truncation."
                    ),
                },
                "label_a": {
                    "type": "string",
                    "description": (
                        "Label for the 'before' side in the diff header "
                        "(default: path_a or 'original')."
                    ),
                    "default": "",
                },
                "label_b": {
                    "type": "string",
                    "description": (
                        "Label for the 'after' side in the diff header "
                        "(default: path_b / 'expected' / 'desired')."
                    ),
                    "default": "",
                },
            },
            "required": ["path_a", "context", "max_lines"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path_a: str,
        context: int,
        max_lines: int,
        path_b: str = "",
        text_b: str = "",
        mode: str = "file_vs_text",
        label_a: str = "",
        label_b: str = "",
    ) -> ToolResult:
        # ------------------------------------------------------------------ #
        # 1. Validate and read path_a
        # ------------------------------------------------------------------ #
        path_result_a = self._check_path(config, path_a)
        if isinstance(path_result_a, ToolResult):
            return path_result_a  # This is a security or validation error
        resolved_a = path_result_a  # This is the validated path string

        p_a = Path(resolved_a)
        if not p_a.exists():
            return ToolResult(error=f"File not found: {resolved_a}", is_error=True)
        if not p_a.is_file():
            return ToolResult(error=f"Not a file: {resolved_a}", is_error=True)

        try:
            content_a = p_a.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(error=f"Could not read {resolved_a}: {exc}", is_error=True)

        # ------------------------------------------------------------------ #
        # 2. Obtain the 'b' side
        # ------------------------------------------------------------------ #
        if mode == "file_vs_file":
            if not path_b:
                return ToolResult(
                    error="mode='file_vs_file' requires path_b to be supplied",
                    is_error=True,
                )
            resolved_b = self._check_path(config, path_b)
            if isinstance(resolved_b, ToolResult):
                return resolved_b

            p_b = Path(resolved_b)
            if not p_b.exists():
                return ToolResult(error=f"File not found: {resolved_b}", is_error=True)
            if not p_b.is_file():
                return ToolResult(error=f"Not a file: {resolved_b}", is_error=True)

            try:
                content_b = p_b.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ToolResult(error=f"Could not read {resolved_b}: {exc}", is_error=True)

            from_label = label_a or str(p_a)
            to_label = label_b or str(p_b)

        elif mode == "file_vs_text":
            # text_b may be empty string (valid — diffing against empty is how
            # you see "what would deleting this file look like").
            content_b = text_b
            from_label = label_a or str(p_a)
            to_label = label_b or "expected"

        else:
            return ToolResult(
                error=(
                    f"Unknown mode {mode!r}. "
                    "Use 'file_vs_text' or 'file_vs_file'."
                ),
                is_error=True,
            )

        # ------------------------------------------------------------------ #
        # 3. Compute unified diff
        # ------------------------------------------------------------------ #
        # splitlines(keepends=True) is required by difflib.unified_diff so
        # that newline endings are preserved in the hunk output.
        lines_a = content_a.splitlines(keepends=True)
        lines_b = content_b.splitlines(keepends=True)

        # Clamp context to a sane range to prevent difflib from hanging on
        # a pathologically large context value.
        context_clamped = max(0, min(context, 20))

        diff_iter = difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=from_label,
            tofile=to_label,
            n=context_clamped,
            # lineterm="" means difflib does NOT add an extra \n at the end
            # of each output line — the lines already have \n from splitlines.
            # Without this we get double newlines in the output.
            lineterm="",
        )

        diff_lines = list(diff_iter)

        # ------------------------------------------------------------------ #
        # 4. Handle no-difference case
        # ------------------------------------------------------------------ #
        if not diff_lines:
            return ToolResult(
                output=(
                    f"No differences between {from_label!r} and {to_label!r}.\n"
                    f"Files are identical ({len(lines_a)} line(s))."
                )
            )

        # ------------------------------------------------------------------ #
        # 5. Truncate if necessary and build output
        # ------------------------------------------------------------------ #
        truncated = False
        if len(diff_lines) > max_lines:
            diff_lines = diff_lines[:max_lines]
            truncated = True

        # Count changed lines for the summary header (+ additions, - removals)
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

        summary = (
            f"diff {from_label!r} \u2192 {to_label!r}  "
            f"[+{added} line(s), -{removed} line(s)]"
        )
        if truncated:
            summary += f"  [output truncated to {max_lines} lines \u2014 diff is larger]"

        output = summary + "\n" + "".join(diff_lines)
        return ToolResult(output=output)
