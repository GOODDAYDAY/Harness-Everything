"""batch_edit — apply many search/replace edits in a single tool call.

Primary tool for code modification. Each edit is independent: one failing
edit does not abort the rest. Every path flows through
``FileSecurity.atomic_validate_and_read`` + ``atomic_validate_and_write``
(same symlink / allowed_paths / phase-scope checks as :class:`EditFileTool`).

Edits to the *same* path must run serially to avoid read-modify-write
races, so we group by path and parallelise only across groups.
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
class BatchEditTool(Tool):
    name = "batch_edit"
    description = (
        "Primary tool for code modification. Applies up to 100 search/replace "
        "edits in a single call. Each edit specifies path/old_str/new_str "
        "(optionally replace_all). old_str must appear exactly once unless "
        "replace_all=true. Edits are independent — partial failures are reported "
        "per-edit and do not abort the batch. Prefer this over edit_file: "
        "one LLM round-trip can land a coherent multi-file change."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    MAX_EDITS = 100

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": (
                        f"List of edit operations (max {self.MAX_EDITS}). "
                        "Each item requires: 'path' (file to edit), 'old_str' "
                        "(exact text to find — must be unique in the file), "
                        "'new_str' (replacement text). Optional: 'replace_all' "
                        "(bool, default false — set true to replace every occurrence). "
                        "Partial failures are reported per-edit without aborting the batch."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "File to edit (must exist).",
                            },
                            "old_str": {
                                "type": "string",
                                "description": (
                                    "Exact text to find — must match character-for-character "
                                    "including all whitespace and indentation. Must appear "
                                    "exactly once unless replace_all=true. Empty string not "
                                    "allowed. Match failures are almost always due to "
                                    "whitespace/indentation differences; copy-paste from "
                                    "batch_read output to guarantee an exact match."
                                ),
                            },
                            "new_str": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": (
                                    "If true, replace every occurrence; "
                                    "if false (default), require exactly one match."
                                ),
                                "default": False,
                            },
                        },
                        "required": ["path", "old_str", "new_str"],
                    },
                },
            },
            "required": ["edits"],
        }

    async def _apply_one(
        self, config: HarnessConfig, label: str, edit: dict[str, Any],
    ) -> tuple[bool, str, str]:
        """Apply one edit; return (ok, result_line, resolved_path_or_empty)."""
        path = edit.get("path") or ""
        old_str = edit.get("old_str") or ""
        new_str = edit.get("new_str", "")
        replace_all = bool(edit.get("replace_all", False))

        if not path:
            return False, f"{label} ERROR: missing 'path'", ""
        if not old_str:
            return False, f"{label} {path}: ERROR: 'old_str' must be non-empty", ""

        read_out = handle_atomic_result(
            await self.file_security.atomic_validate_and_read(
                config, path,
                require_exists=True, check_scope=True, resolve_symlinks=False,
            ),
            metadata_keys=("text", "resolved_path"),
        )
        if read_out.is_error:
            return False, f"{label} {path}: ERROR reading: {read_out.error}", ""
        text = read_out.metadata["text"]
        resolved = read_out.metadata["resolved_path"]

        count = text.count(old_str)
        if count == 0:
            return (
                False,
                (
                    f"{label} {path}: ERROR: old_str not found "
                    "(check whitespace/indentation — use batch_read to copy exact text)"
                ),
                "",
            )
        if count > 1 and not replace_all:
            return (
                False,
                (
                    f"{label} {path}: ERROR: old_str appears {count} times; "
                    "set replace_all=true or provide more context"
                ),
                "",
            )

        new_text = (
            text.replace(old_str, new_str)
            if replace_all else text.replace(old_str, new_str, 1)
        )
        replaced = count if replace_all else 1

        write_out = handle_atomic_result(
            await self.file_security.atomic_validate_and_write(
                config, resolved, new_text,
                require_exists=False, check_scope=True, resolve_symlinks=False,
            ),
            metadata_keys=(),
        )
        if write_out.is_error:
            return False, f"{label} {path}: ERROR writing: {write_out.error}", resolved

        return (
            True,
            f"{label} {path}: OK ({replaced} replacement{'s' if replaced != 1 else ''})",
            resolved,
        )

    async def execute(
        self, config: HarnessConfig, *, edits: list[dict[str, Any]],
    ) -> ToolResult:
        if not edits:
            return ToolResult(
                error="edits must be a non-empty list of edit objects",
                is_error=True,
            )
        if len(edits) > self.MAX_EDITS:
            return ToolResult(
                error=(
                    f"edits has {len(edits)} entries; cap is {self.MAX_EDITS} per call. "
                    "Split into multiple batch_edit calls."
                ),
                is_error=True,
            )

        # Group by path so same-path edits stay sequential (no read-modify-write
        # races); edits on different paths can run in parallel.
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for i, edit in enumerate(edits, 1):
            path = edit.get("path") or ""
            groups.setdefault(path, []).append((i, edit))

        async def _apply_group(
            entries: list[tuple[int, dict[str, Any]]],
        ) -> list[tuple[int, bool, str, str]]:
            results: list[tuple[int, bool, str, str]] = []
            for i, edit in entries:
                ok, line, resolved = await self._apply_one(config, f"[{i}]", edit)
                results.append((i, ok, line, resolved))
            return results

        group_outputs = await asyncio.gather(*(
            _apply_group(entries) for entries in groups.values()
        ))

        flat = [item for group in group_outputs for item in group]
        flat.sort(key=lambda r: r[0])   # restore declaration order in the report

        lines: list[str] = []
        n_ok = 0
        n_err = 0
        changed_paths: set[str] = set()
        for _, ok, line, resolved in flat:
            lines.append(line)
            if ok:
                n_ok += 1
                if resolved:
                    changed_paths.add(resolved)
            else:
                n_err += 1

        summary = f"batch_edit: {n_ok}/{len(edits)} succeeded"
        if n_err:
            summary += f", {n_err} failed"
        output = summary + "\n" + "\n".join(lines)
        return ToolResult(
            output=output,
            metadata={
                "n_ok": n_ok,
                "n_err": n_err,
                "changed_paths": sorted(changed_paths),
            },
        )
