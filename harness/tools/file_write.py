"""write_file — create or overwrite a file."""

from __future__ import annotations

from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation, handle_atomic_result


@enforce_atomic_validation
class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing file with the "
        "given content. WARNING: replaces the entire file — all prior content "
        "is lost. For partial changes use edit_file (single file) or "
        "batch_edit (multi-file). For writing multiple new files at once, "
        "prefer batch_write."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to create or overwrite (directories are created automatically)"},
                "content": {"type": "string", "description": "Complete new file content. Replaces the entire file — do NOT omit unchanged sections."},
            },
            "required": ["path", "content"],
        }

    async def execute(
        self, config: HarnessConfig, *, path: str, content: str
    ) -> ToolResult:
        # Use consolidated atomic validation and write
        result = await self.file_security.atomic_validate_and_write(
            config, path, content, require_exists=False, check_scope=True, resolve_symlinks=False
        )
        # Use centralized handler for atomic validation results
        return handle_atomic_result(result, metadata_keys=())


