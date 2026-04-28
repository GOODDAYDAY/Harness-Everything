"""batch_read — read multiple files in a single tool call.

Primary reader for agent code exploration. Each path flows through
``FileSecurity.atomic_validate_and_read`` (same symlink / allowed_paths /
phase-scope checks as :class:`ReadFileTool`). Per-file failures are
reported per-section without aborting the batch.
"""

from __future__ import annotations

import asyncio
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
        "Primary tool for reading files — use this for everything, even a single "
        "file. Reads one or many files in a single call (one LLM round-trip). "
        "Much faster than read_file or bash. Supports line ranges via "
        "offset/limit if you only need a section. "
        "Always use this instead of read_file. "
        "By default reads up to 2000 lines per file — just pass the paths, "
        "no need to specify limit unless you want a specific range."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    # Per-call caps — tuned so a single response stays well under typical
    # context windows. The MAX_TOTAL_CHARS budget truncates the output
    # after the reads complete, so the cap is a safety net not a correctness
    # invariant.
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
                        f"Max lines per file. Default {self.MAX_LINES_PER_FILE} "
                        "(reads the whole file in most cases). "
                        "Only set this if you need a specific range with offset."
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

    async def _read_one(
        self, config: HarnessConfig, raw_path: str, offset: int, limit: int,
    ) -> tuple[bool, str]:
        """Validate + read one file. Returns (ok, section_text).

        ``section_text`` always includes the leading ``--- <path> ...`` header
        so the batch output is consistently formatted whether the read
        succeeded or failed.
        """
        if not raw_path:
            return False, f"--- [invalid path: {raw_path!r}] ---\nERROR: empty path\n"

        atomic_result = await self.file_security.atomic_validate_and_read(
            config, raw_path,
            require_exists=True, check_scope=False, resolve_symlinks=False,
        )
        per_file = handle_atomic_result(
            atomic_result, metadata_keys=("text", "resolved_path"),
        )
        if per_file.is_error:
            return False, f"--- {raw_path} ---\nERROR: {per_file.error}\n"

        text = per_file.metadata["text"]
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        if offset > total_lines + 1 or (total_lines == 0 and offset > 1):
            return False, (
                f"--- {raw_path} ---\n"
                f"ERROR: offset {offset} exceeds file length {total_lines}\n"
            )

        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        end_line = start + len(selected)
        numbered = "".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)
        ) if selected else ""

        section = (
            f"--- {raw_path} [lines {start + 1}-{end_line} of {total_lines}] ---\n"
            f"{numbered}"
        )
        if not section.endswith("\n"):
            section += "\n"
        return True, section

    async def execute(
        self,
        config: HarnessConfig,
        *,
        paths: list[str],
        limit: int = 2000,
        offset: int = 1,
    ) -> ToolResult:
        if offset < 1:
            return ToolResult(error=f"offset must be >= 1, got {offset}", is_error=True)
        if limit < 1 or limit > self.MAX_LINES_PER_FILE:
            return ToolResult(
                error=(
                    f"limit must be in [1, {self.MAX_LINES_PER_FILE}], got {limit}"
                ),
                is_error=True,
            )
        if not paths:
            return ToolResult(
                error="paths must be a non-empty list of strings", is_error=True,
            )
        if len(paths) > self.MAX_FILES:
            return ToolResult(
                error=(
                    f"paths has {len(paths)} entries; cap is {self.MAX_FILES} per call. "
                    "Split into multiple batch_read calls."
                ),
                is_error=True,
            )

        # Read all files in parallel — reads are pure I/O, no shared state to
        # race on. Order is preserved because asyncio.gather returns results
        # in task-order, and we assemble the output in that order below.
        outcomes = await asyncio.gather(*(
            self._read_one(config, p, offset, limit) for p in paths
        ))

        sections: list[str] = []
        total_chars = 0
        n_ok = 0
        n_err = 0
        files_skipped_budget = 0
        for ok, section in outcomes:
            if total_chars >= self.MAX_TOTAL_CHARS:
                files_skipped_budget += 1
                continue
            sections.append(section)
            total_chars += len(section)
            if ok:
                n_ok += 1
            else:
                n_err += 1

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
