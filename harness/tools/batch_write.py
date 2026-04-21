"""batch_write — create or overwrite many files in a single tool call.

Primary tool for bulk file creation (new tests, new modules, scaffolding).
Each file is written independently; a per-file failure does not abort the
rest.

Security
--------
Every path flows through ``FileSecurity.atomic_validate_and_write``, so
symlink / allowed_paths / phase-scope guards apply per-file as they do
for :class:`WriteFileTool`.
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
                        f"List of files to write (max {self.MAX_FILES})."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Destination path (created if missing, overwritten if present).",
                            },
                            "content": {
                                "type": "string",
                                "description": "Full file content to write.",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            "required": ["files"],
        }

    async def execute(
        self, config: HarnessConfig, *, files: list[dict[str, Any]],
    ) -> ToolResult:
        if not isinstance(files, list) or not files:
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

        total_content = sum(
            len(f.get("content", "")) if isinstance(f, dict) else 0 for f in files
        )
        if total_content > self.MAX_TOTAL_CHARS:
            return ToolResult(
                error=(
                    f"total content {total_content} chars exceeds cap "
                    f"{self.MAX_TOTAL_CHARS}. Split into smaller batches."
                ),
                is_error=True,
            )

        lines: list[str] = []
        n_ok = 0
        n_err = 0
        written_paths: list[str] = []

        for i, item in enumerate(files, 1):
            label = f"[{i}]"
            if not isinstance(item, dict):
                n_err += 1
                lines.append(f"{label} ERROR: item must be an object, got {type(item).__name__}")
                continue
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str) or not path:
                n_err += 1
                lines.append(f"{label} ERROR: missing or invalid 'path'")
                continue
            if not isinstance(content, str):
                n_err += 1
                lines.append(f"{label} {path}: ERROR: 'content' must be a string")
                continue

            write_result = await self.file_security.atomic_validate_and_write(
                config, path, content,
                require_exists=False, check_scope=True, resolve_symlinks=False,
            )
            out = handle_atomic_result(write_result, metadata_keys=())
            if out.is_error:
                n_err += 1
                lines.append(f"{label} {path}: ERROR: {out.error}")
                continue

            n_ok += 1
            written_paths.append(path)
            lines.append(f"{label} {path}: OK ({len(content)} bytes)")

        summary = f"batch_write: {n_ok}/{len(files)} succeeded"
        if n_err:
            summary += f", {n_err} failed"
        output = summary + "\n" + "\n".join(lines)

        return ToolResult(
            output=output,
            metadata={
                "n_ok": n_ok,
                "n_err": n_err,
                "written_paths": written_paths,
            },
        )
