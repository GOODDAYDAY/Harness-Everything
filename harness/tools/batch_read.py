"""batch_read — read multiple files in a single tool call.

This is the primary tool for agent code exploration. A single invocation can
fetch up to ``MAX_FILES`` files (50), each capped at ``MAX_LINES_PER_FILE``
lines (2000), with an overall byte budget of ``MAX_TOTAL_CHARS`` (500 000).

Why batch instead of N × read_file?
-----------------------------------
Each tool call is an LLM round-trip. Reading 20 files one-at-a-time burns
20 turns from the budget; reading them in one call uses 1 turn. Combined
with ``_CachedToolRegistry`` (which dedupes by path + offset + limit), the
batch variant also avoids re-reading files the agent has already seen.

Security
--------
Every path flows through ``FileSecurity.atomic_validate_and_read`` exactly
like :class:`ReadFileTool`, so symlink / allowed_paths / phase-scope checks
apply per-file. A per-file failure is reported in that file's section but
does not abort the whole batch.
"""

from __future__ import annotations

from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import (
    Tool,
    ToolResult,
    enforce_atomic_validation,
    handle_atomic_result,
)


@enforce_atomic_validation
class BatchReadTool(Tool):
    name = "batch_read"
    description = (
        "Primary tool for reading files. Reads multiple files in one call. "
        "Prefer this over read_file — each call is one LLM round-trip, so "
        "reading 10 files here costs the same as reading 1. "
        "Each file returns line-numbered content with a header."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    # Per-call caps — tuned so a single response stays well under typical
    # context windows. 50 × 2000 × ~80 chars/line = 8 MB worst case; the
    # MAX_TOTAL_CHARS hard-cap stops us before that.
    MAX_FILES = 50
    MAX_LINES_PER_FILE = 2000
    MAX_TOTAL_CHARS = 500_000

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"List of file paths to read. Max {self.MAX_FILES} per call. "
                        "Prefer batching 3-10 related files at once over single-file reads."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max lines per file (default {self.MAX_LINES_PER_FILE}). "
                        "All files in this call use the same limit."
                    ),
                    "default": 2000,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Start reading from this line in every file (1-based). "
                        "Default 1. Rarely needed for batch reads."
                    ),
                    "default": 1,
                },
            },
            "required": ["paths"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        paths: list[str],
        limit: int = 2000,
        offset: int = 1,
    ) -> ToolResult:
        # --- argument coercion (LLM sometimes quotes integers) ---
        try:
            offset = int(offset) if offset is not None else 1
            limit = int(limit) if limit is not None else self.MAX_LINES_PER_FILE
        except (TypeError, ValueError) as exc:
            return ToolResult(
                error=f"offset/limit must be integers: {exc}", is_error=True
            )
        if offset < 1:
            return ToolResult(error=f"offset must be >= 1, got {offset}", is_error=True)
        if limit < 1:
            return ToolResult(error=f"limit must be >= 1, got {limit}", is_error=True)
        if limit > self.MAX_LINES_PER_FILE:
            return ToolResult(
                error=(
                    f"limit {limit} exceeds batch_read cap {self.MAX_LINES_PER_FILE}. "
                    "Use read_file for very large single files."
                ),
                is_error=True,
            )

        # --- input validation ---
        if not isinstance(paths, list) or not paths:
            return ToolResult(
                error="paths must be a non-empty list of strings", is_error=True
            )
        if len(paths) > self.MAX_FILES:
            return ToolResult(
                error=(
                    f"paths has {len(paths)} entries; cap is {self.MAX_FILES} per call. "
                    "Split into multiple batch_read calls."
                ),
                is_error=True,
            )

        # --- read loop ---
        sections: list[str] = []
        total_chars = 0
        n_ok = 0
        n_err = 0
        files_skipped_budget = 0

        for raw_path in paths:
            if not isinstance(raw_path, str) or not raw_path:
                n_err += 1
                sections.append(f"--- [invalid path: {raw_path!r}] ---\nERROR: path must be a non-empty string\n")
                continue

            # Stop early if we're over the total-chars budget — report the rest
            # as skipped rather than silently truncating.
            if total_chars >= self.MAX_TOTAL_CHARS:
                files_skipped_budget += 1
                continue

            atomic_result = await self.file_security.atomic_validate_and_read(
                config, raw_path,
                require_exists=True, check_scope=False, resolve_symlinks=False,
            )
            per_file = handle_atomic_result(
                atomic_result, metadata_keys=("text", "resolved_path")
            )
            if per_file.is_error:
                n_err += 1
                sections.append(
                    f"--- {raw_path} ---\nERROR: {per_file.error}\n"
                )
                continue

            text = per_file.metadata["text"]
            resolved = per_file.metadata["resolved_path"]
            lines = text.splitlines(keepends=True)
            total_lines = len(lines)

            if offset > total_lines + 1 or (total_lines == 0 and offset > 1):
                n_err += 1
                sections.append(
                    f"--- {raw_path} ---\n"
                    f"ERROR: offset {offset} exceeds file length {total_lines}\n"
                )
                continue

            start = max(offset - 1, 0)
            selected = lines[start : start + limit]
            end_line = start + len(selected)

            if selected:
                numbered = "".join(
                    f"{start + i + 1:>6}\t{line}"
                    for i, line in enumerate(selected)
                )
            else:
                numbered = ""

            header = f"--- {raw_path} [lines {start + 1}-{end_line} of {total_lines}] ---\n"
            section = header + numbered
            if not section.endswith("\n"):
                section += "\n"

            sections.append(section)
            total_chars += len(section)
            n_ok += 1

        # --- summary + output ---
        summary_parts = [f"Read {n_ok}/{len(paths)} file(s)"]
        if n_err:
            summary_parts.append(f"{n_err} error(s)")
        if files_skipped_budget:
            summary_parts.append(
                f"{files_skipped_budget} skipped (exceeded {self.MAX_TOTAL_CHARS}-char budget)"
            )
        summary = ", ".join(summary_parts) + f"  [{total_chars} chars total]"
        output = summary + "\n\n" + "\n".join(sections)

        return ToolResult(output=output, metadata={"n_ok": n_ok, "n_err": n_err})
