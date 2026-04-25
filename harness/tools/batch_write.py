"""batch_write — create or overwrite many files in a single tool call.

Primary tool for bulk file creation. Each path flows through
``FileSecurity.atomic_validate_and_write`` (same guards as
:class:`WriteFileTool`). Per-file failures are reported without aborting
the batch. Writes run in parallel since different paths don't race.
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
class BatchWriteTool(Tool):
    name = "batch_write"
    description = (
        "Primary tool for creating or overwriting files. Writes up to 50 "
        "files in a single call. Each item specifies path + content. "
        "Partial failures are reported per-file; the rest proceed. Prefer "
        "this over write_file for scaffolding (new modules, test files, "
        "generated config) — one LLM round-trip writes the entire set."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    MAX_FILES = 50
    MAX_TOTAL_CHARS = 1_000_000  # 1 MB total write budget per call

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": (
                        f"List of files to write (max {self.MAX_FILES}). "
                        "Each item must have: 'path' (destination file path) "
                        "and 'content' (complete file content — replaces entire file). "
                        "Partial failures are reported per-file; others still proceed."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Destination file path (parent directories created automatically; existing file is overwritten in full).",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete file content to write. The entire file is replaced — do NOT omit unchanged sections.",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            "required": ["files"],
        }

    async def _write_one(
        self, config: HarnessConfig, label: str, item: dict[str, Any],
    ) -> tuple[bool, str, str]:
        """Write one file; return (ok, result_line, path)."""
        path = item.get("path") or ""
        content = item.get("content", "")
        if not path:
            return False, f"{label} ERROR: missing 'path'", ""
        out = handle_atomic_result(
            await self.file_security.atomic_validate_and_write(
                config, path, content,
                require_exists=False, check_scope=True, resolve_symlinks=False,
            ),
            metadata_keys=(),
        )
        if out.is_error:
            return False, f"{label} {path}: ERROR: {out.error}", path
        return True, f"{label} {path}: OK ({len(content)} bytes)", path

    async def execute(
        self, config: HarnessConfig, *, files: list[dict[str, Any]],
    ) -> ToolResult:
        if not files:
            return ToolResult(
                error="files must be a non-empty list of {path, content} objects",
                is_error=True,
            )
        if len(files) > self.MAX_FILES:
            return ToolResult(
                error=(
                    f"files has {len(files)} entries; cap is {self.MAX_FILES} per call. "
                    "Split into multiple batch_write calls."
                ),
                is_error=True,
            )
        total_content = sum(len(f.get("content", "")) for f in files)
        if total_content > self.MAX_TOTAL_CHARS:
            return ToolResult(
                error=(
                    f"total content {total_content} chars exceeds cap "
                    f"{self.MAX_TOTAL_CHARS}. Split into smaller batches."
                ),
                is_error=True,
            )

        # Parallel writes — different paths don't race on disk. Same-path
        # duplicates in one call are pathological and handled by last-write-wins.
        outcomes = await asyncio.gather(*(
            self._write_one(config, f"[{i}]", item)
            for i, item in enumerate(files, 1)
        ))

        lines = [line for _, line, _ in outcomes]
        n_ok = sum(1 for ok, _, _ in outcomes if ok)
        n_err = len(outcomes) - n_ok
        written_paths = [p for ok, _, p in outcomes if ok and p]

        summary = f"batch_write: {n_ok}/{len(files)} succeeded"
        if n_err:
            summary += f", {n_err} failed"
        return ToolResult(
            output=summary + "\n" + "\n".join(lines),
            metadata={
                "n_ok": n_ok,
                "n_err": n_err,
                "written_paths": written_paths,
            },
        )
