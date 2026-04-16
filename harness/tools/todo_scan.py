"""todo_scan — scan source files for TODO/FIXME/HACK/NOTE/BUG/XXX annotations.

Finds developer annotations left in code comments and docstrings, groups
them by tag and file, and returns structured output useful for:

* Code-quality audits — see all outstanding TODO items in one call.
* Pre-release checklists — find all FIXME/BUG items before shipping.
* Context gathering — find NOTE/HACK items before modifying a module to
  understand intentional workarounds.

Usage examples
--------------
Agent usage (via tool call)::

    todo_scan()
    todo_scan(tags=["FIXME", "BUG"])
    todo_scan(root="harness/tools", file_glob="**/*.py")
    todo_scan(tags=["TODO", "FIXME"], sort_by="file")

Parameters
----------
* ``root``       — directory to search (default: config.workspace).
* ``file_glob``  — glob pattern selecting files (default: ``**/*.py``).
* ``tags``       — list of annotation tags to find (default: all six built-in
                   tags: TODO, FIXME, HACK, NOTE, BUG, XXX).
* ``sort_by``    — ``"file"`` (default) | ``"tag"`` | ``"line"`` controls how
                   results are ordered in the output.
* ``include_context`` — include one line of context before and after each
                   annotation (default: false).
* ``max_results``— max total annotations to return (default: 200, max: 1000).

Output
------
JSON structure::

    {
      "root": "/path/to/workspace",
      "files_scanned": 42,
      "total_found": 17,
      "by_tag": {"TODO": 10, "FIXME": 5, "BUG": 2},
      "results": [
        {
          "file": "harness/loop.py",
          "line": 42,
          "tag": "TODO",
          "text": "TODO: add retry logic for transient failures",
          "context_before": "    for attempt in range(max_retries):",
          "context_after": "    raise MaxRetriesExceeded()"
        },
        ...
      ]
    }

Implementation notes
--------------------
* Annotations are found by scanning each line for the pattern
  ``# <TAG>[:(]`` or ``# <TAG> `` (case-insensitive by default).
* Inline comments (``# ...``) and block comments are both found.
* Docstrings are scanned via plain-text search (no AST needed) to keep the
  implementation simple and handle files with syntax errors.
* Security: uses ``_check_dir_root`` + ``_rglob_safe`` — null-byte rejection,
  ``PERMISSION ERROR`` prefix, allowed-paths enforcement, symlink-safe traversal.
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

_DEFAULT_TAGS: list[str] = ["TODO", "FIXME", "HACK", "NOTE", "BUG", "XXX"]
_DEFAULT_FILE_GLOB: str = "**/*.py"
_DEFAULT_MAX_RESULTS: int = 200
_MAX_RESULTS_HARD_CAP: int = 1000

# Regex for matching a tag annotation at the beginning of or inside a comment.
# Matches: "# TODO", "# TODO:", "# TODO(user):", "# FIXME -", "# BUG "
# Group 1 = the tag name.
_TAG_PATTERN_TEMPLATE = r"#\s*({tags})\s*[:()\-\s]"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-testable)
# ---------------------------------------------------------------------------


def _build_tag_regex(tags: list[str], case_insensitive: bool) -> "re.Pattern[str]":
    """Build a compiled regex that matches any of the given tags in a comment."""
    joined = "|".join(re.escape(t) for t in tags)
    pattern = _TAG_PATTERN_TEMPLATE.format(tags=joined)
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(pattern, flags)


def _extract_tag(line: str, regex: "re.Pattern[str]", tags: list[str]) -> str | None:
    """Return the matched tag name (uppercased) if the line contains a tag annotation."""
    m = regex.search(line)
    if m is None:
        return None
    matched = m.group(1).upper()
    # Normalise to canonical form (e.g. "fixme" -> "FIXME")
    for tag in tags:
        if tag.upper() == matched:
            return tag
    return matched  # fallback: return as-is if not in list


def _scan_file(
    fpath: Path,
    regex: "re.Pattern[str]",
    tags: list[str],
    include_context: bool,
    max_results: int,
) -> list[dict[str, Any]]:
    """Scan a single file and return annotation records."""
    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    results: list[dict[str, Any]] = []

    for i, line in enumerate(lines):
        if len(results) >= max_results:
            break
        tag = _extract_tag(line, regex, tags)
        if tag is None:
            continue

        record: dict[str, Any] = {
            "tag": tag,
            "line": i + 1,  # 1-based
            "text": line.strip(),
        }

        if include_context:
            before = lines[i - 1].strip() if i > 0 else ""
            after = lines[i + 1].strip() if i + 1 < len(lines) else ""
            record["context_before"] = before
            record["context_after"] = after

        results.append(record)

    return results


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class TodoScanTool(Tool):
    """Scan source files for TODO/FIXME/HACK/NOTE/BUG/XXX annotations.

    Returns a structured JSON list of all annotation hits grouped by file,
    with tag counts and optional surrounding context lines.
    """

    name = "todo_scan"
    description = (
        "Scan source files for developer annotation comments: "
        "TODO, FIXME, HACK, NOTE, BUG, XXX (configurable). "
        "Returns structured JSON with file, line number, tag, and comment text. "
        "Useful for code-quality audits, pre-release checklists, and context "
        "gathering before modifying code with known workarounds."
    )
    requires_path_check = False  # manual _check_dir_root enforcement
    tags = frozenset({"search"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": (
                        "Root directory to search (default: config.workspace). "
                        "Relative paths are resolved against the workspace."
                    ),
                    "default": "",
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern selecting which files to scan "
                        "(default: '**/*.py'). "
                        "E.g. '**/*.py', 'src/**/*.ts', '**/*.js'."
                    ),
                    "default": _DEFAULT_FILE_GLOB,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Annotation tags to find (default: all six: "
                        "TODO, FIXME, HACK, NOTE, BUG, XXX). "
                        "Tags are matched case-insensitively. "
                        "Example: [\"TODO\", \"FIXME\"] to find only urgent items."
                    ),
                    "default": _DEFAULT_TAGS,
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["file", "tag", "line"],
                    "description": (
                        "Sort order for results: 'file' (default — group by file, "
                        "then line), 'tag' (group by tag, then file), "
                        "'line' (sort globally by line number within file)."
                    ),
                    "default": "file",
                },
                "include_context": {
                    "type": "boolean",
                    "description": (
                        "Include one line of source context before and after each "
                        "annotation (default: false). Adds 'context_before' and "
                        "'context_after' fields to each result."
                    ),
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"Maximum total annotations to return "
                        f"(default: {_DEFAULT_MAX_RESULTS}, "
                        f"max: {_MAX_RESULTS_HARD_CAP})."
                    ),
                    "default": _DEFAULT_MAX_RESULTS,
                    "minimum": 1,
                    "maximum": _MAX_RESULTS_HARD_CAP,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        root: str = "",
        file_glob: str = _DEFAULT_FILE_GLOB,
        tags: list[str] | None = None,
        sort_by: str = "file",
        include_context: bool = False,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> ToolResult:
        # ------------------------------------------------------------------ #
        # 1. Validate / normalise inputs
        # ------------------------------------------------------------------ #
        effective_tags: list[str] = tags if tags else list(_DEFAULT_TAGS)
        # Deduplicate and uppercase
        seen: set[str] = set()
        clean_tags: list[str] = []
        for t in effective_tags:
            up = t.upper().strip()
            if up and up not in seen:
                seen.add(up)
                clean_tags.append(up)
        if not clean_tags:
            return ToolResult(
                error="tags list must not be empty", is_error=True
            )

        # Validate sort_by
        valid_sorts = ("file", "tag", "line")
        if sort_by not in valid_sorts:
            return ToolResult(
                error=(
                    f"Invalid sort_by {sort_by!r}. "
                    f"Valid values: {', '.join(valid_sorts)}"
                ),
                is_error=True,
            )

        # Clamp max_results
        max_results = max(1, min(_MAX_RESULTS_HARD_CAP, int(max_results)))

        # ------------------------------------------------------------------ #
        # 2. Resolve and validate root directory
        # ------------------------------------------------------------------ #
        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        # ------------------------------------------------------------------ #
        # 3. Build tag regex
        # ------------------------------------------------------------------ #
        regex = _build_tag_regex(clean_tags, case_insensitive=True)

        # ------------------------------------------------------------------ #
        # 4. Collect files
        # ------------------------------------------------------------------ #
        all_files = self._rglob_safe(search_root, file_glob, allowed, limit=2000)

        # ------------------------------------------------------------------ #
        # 5. Scan files
        # ------------------------------------------------------------------ #
        # Each entry: {"file": rel_path, "line": N, "tag": TAG, "text": "..."}
        all_hits: list[dict[str, Any]] = []
        files_scanned = 0
        total_remaining = max_results

        for fpath in all_files:
            if total_remaining <= 0:
                break
            hits = _scan_file(
                fpath,
                regex,
                clean_tags,
                include_context,
                total_remaining,
            )
            if hits:
                try:
                    rel = str(fpath.relative_to(search_root))
                except ValueError:
                    rel = str(fpath)
                for h in hits:
                    h["file"] = rel
                all_hits.extend(hits)
                total_remaining -= len(hits)
            files_scanned += 1

        # ------------------------------------------------------------------ #
        # 6. Sort results
        # ------------------------------------------------------------------ #
        if sort_by == "file":
            all_hits.sort(key=lambda h: (h["file"], h["line"]))
        elif sort_by == "tag":
            all_hits.sort(key=lambda h: (h["tag"], h["file"], h["line"]))
        else:  # "line" — sort by file then line (global line ordering within file)
            all_hits.sort(key=lambda h: (h["file"], h["line"]))

        # ------------------------------------------------------------------ #
        # 7. Build by_tag counts
        # ------------------------------------------------------------------ #
        by_tag: dict[str, int] = {}
        for h in all_hits:
            by_tag[h["tag"]] = by_tag.get(h["tag"], 0) + 1

        # ------------------------------------------------------------------ #
        # 8. Build output
        # ------------------------------------------------------------------ #
        truncated = total_remaining <= 0

        result_dict: dict[str, Any] = {
            "root": str(search_root),
            "files_scanned": files_scanned,
            "total_found": len(all_hits),
            "by_tag": by_tag,
            "tags_searched": clean_tags,
            "truncated": truncated,
            "results": all_hits,
        }

        return ToolResult(output=self._safe_json(result_dict, max_bytes=32_000))
