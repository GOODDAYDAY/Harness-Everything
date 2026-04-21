"""batch_edit — apply many search/replace edits in a single tool call.

Primary tool for code modification. Each edit is independent: one failing
edit does not abort the rest. Report format lists per-edit outcome so the
LLM can retry only the failed ones.

Security
--------
Every edit path goes through ``FileSecurity.atomic_validate_and_read``
(for the before-text) and ``FileSecurity.atomic_validate_and_write`` (for
the after-text). Per-file symlink, allowed_paths, and phase-scope checks
are enforced identically to :class:`EditFileTool`.
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
                        f"List of edit operations (max {self.MAX_EDITS})."
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
                                    "Exact text to find. Must match exactly once "
                                    "unless replace_all=true. Empty string not allowed."
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

    async def execute(
        self, config: HarnessConfig, *, edits: list[dict[str, Any]],
    ) -> ToolResult:
        if not isinstance(edits, list) or not edits:
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

        lines: list[str] = []
        n_ok = 0
        n_err = 0
        changed_paths: set[str] = set()

        for i, edit in enumerate(edits, 1):
            label = f"[{i}]"
            if not isinstance(edit, dict):
                n_err += 1
                lines.append(f"{label} ERROR: edit must be an object, got {type(edit).__name__}")
                continue

            path = edit.get("path")
            old_str = edit.get("old_str")
            new_str = edit.get("new_str")
            replace_all = bool(edit.get("replace_all", False))

            if not isinstance(path, str) or not path:
                n_err += 1
                lines.append(f"{label} ERROR: missing or invalid 'path'")
                continue
            if not isinstance(old_str, str) or old_str == "":
                n_err += 1
                lines.append(f"{label} {path}: ERROR: 'old_str' must be a non-empty string")
                continue
            if not isinstance(new_str, str):
                n_err += 1
                lines.append(f"{label} {path}: ERROR: 'new_str' must be a string")
                continue

            # --- read ---
            read_result = await self.file_security.atomic_validate_and_read(
                config, path,
                require_exists=True, check_scope=True, resolve_symlinks=False,
            )
            read_out = handle_atomic_result(
                read_result, metadata_keys=("text", "resolved_path"),
            )
            if read_out.is_error:
                n_err += 1
                lines.append(f"{label} {path}: ERROR reading: {read_out.error}")
                continue
            text = read_out.metadata["text"]
            resolved = read_out.metadata["resolved_path"]

            # --- locate + apply ---
            count = text.count(old_str)
            if count == 0:
                n_err += 1
                lines.append(f"{label} {path}: ERROR: old_str not found")
                continue
            if count > 1 and not replace_all:
                n_err += 1
                lines.append(
                    f"{label} {path}: ERROR: old_str appears {count} times; "
                    "set replace_all=true or provide more context"
                )
                continue

            if replace_all:
                new_text = text.replace(old_str, new_str)
                replaced = count
            else:
                new_text = text.replace(old_str, new_str, 1)
                replaced = 1

            # --- write ---
            write_result = await self.file_security.atomic_validate_and_write(
                config, resolved, new_text,
                require_exists=False, check_scope=True, resolve_symlinks=False,
            )
            write_out = handle_atomic_result(write_result, metadata_keys=())
            if write_out.is_error:
                n_err += 1
                lines.append(f"{label} {path}: ERROR writing: {write_out.error}")
                continue

            n_ok += 1
            changed_paths.add(resolved)
            lines.append(f"{label} {path}: OK ({replaced} replacement{'s' if replaced != 1 else ''})")

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
