"""edit_file — search/replace within a file."""

from __future__ import annotations

from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult, enforce_atomic_validation, handle_atomic_result


@enforce_atomic_validation
class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Surgical single-file search/replace. "
        "old_str must match exactly once unless replace_all=true. "
        "For changes spanning multiple files use batch_edit instead. "
        "To preview without writing, set dry_run=true."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit (must exist)"},
                "old_str": {
                    "type": "string",
                    "description": "Exact text to replace — must match character-for-character including all whitespace and indentation. Must appear exactly once (raises error if 0 or 2+ matches). Use replace_all=true to replace every occurrence. Match failures are almost always whitespace/indentation differences; copy-paste from batch_read output.",
                },
                "new_str": {"type": "string", "description": "Replacement text that replaces old_str. Use empty string \"\" to delete old_str."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false)",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview changes without writing to disk.",
                    "default": False,
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    def _calculate_changes(
        self, text: str, old_str: str, new_str: str, replace_all: bool
    ) -> tuple[str, int, list[tuple[int, str, str]]]:
        """Calculate the changes that would be made to the text.
        
        Returns:
            tuple of (new_text, replacement_count, changes_preview)
            where changes_preview is a list of (line_number, old_line, new_line)
        """
        if old_str == "":
            # Special handling for empty string replacement
            if text == "":
                # Empty-to-empty: count = 0 (no replacement needed)
                # Empty-to-non-empty: count = 1 (insert at position 0)
                count = 1 if new_str != "" else 0
            else:
                # Non-empty text with empty old_str
                if new_str == "":
                    # Empty-to-empty: count = 0 (no replacement needed)
                    count = 0
                else:
                    # Empty-to-non-empty
                    if replace_all:
                        # Replace at all positions: before each character and at the end
                        count = len(text) + 1
                    else:
                        # Replace only at first position (beginning of text)
                        count = 1
        else:
            count = text.count(old_str)
        
        # Generate new text
        if replace_all:
            new_text = text.replace(old_str, new_str)
        else:
            if count > 0:
                new_text = text.replace(old_str, new_str, 1)
            else:
                new_text = text
        
        # Generate preview of changes
        changes_preview = []
        if count > 0:
            lines = text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            
            # For simplicity, we'll show changes at line level
            # This is a simplified preview - in a real implementation,
            # we might want to show more granular changes within lines
            for i, (old_line, new_line) in enumerate(zip(lines, new_lines)):
                if old_line != new_line:
                    changes_preview.append((i + 1, old_line.rstrip('\n'), new_line.rstrip('\n')))
            
            # Handle case where number of lines changes
            if len(lines) != len(new_lines):
                # If lines were added/removed, show the affected area
                min_len = min(len(lines), len(new_lines))
                for i in range(min_len, len(lines)):
                    changes_preview.append((i + 1, lines[i].rstrip('\n'), ""))
                for i in range(min_len, len(new_lines)):
                    changes_preview.append((i + 1, "", new_lines[i].rstrip('\n')))
        
        return new_text, count, changes_preview

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        dry_run: bool = False,
    ) -> ToolResult:
        # Validate empty string replacement before reading file
        if old_str == "" and not replace_all and new_str != "":
            return ToolResult(
                error="Empty string replacement with new_str != '' requires replace_all=True because empty string matches at every position (before each character and at the end). Use replace_all=True to replace all matches, or provide a non-empty old_str.",
                is_error=True
            )
        
        # Use consolidated atomic validation and read
        read_result = await self.file_security.atomic_validate_and_read(
            config, path, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        # Use centralized handler for atomic validation results
        result = handle_atomic_result(read_result, metadata_keys=("text", "resolved_path"))
        if result.is_error:
            return result
        # Extract data from successful result
        text = result.metadata["text"]
        resolved = result.metadata["resolved_path"]
        
        # Check phase scope (phase_edit_globs restriction)
        if scope_err := self._check_phase_scope(config, resolved):
            return scope_err
        
        # Calculate changes using helper method
        new_text, count, changes_preview = self._calculate_changes(
            text, old_str, new_str, replace_all
        )

        if count == 0:
            # Special case: empty-to-empty replacement is always a no-op
            if old_str == "" and new_str == "":
                # This is a valid no-op operation regardless of replace_all
                pass
            else:
                return ToolResult(
                    error=(
                        "old_str not found in file "
                        "(check whitespace/indentation — use batch_read to copy exact text)"
                    ),
                    is_error=True,
                )
        if count > 1 and not replace_all:
            # Find line numbers where old_str appears for better error messages
            lines = text.splitlines(keepends=True)
            line_numbers = []
            for i, line in enumerate(lines, 1):
                if old_str in line:
                    line_numbers.append(i)
            
            line_info = f" on lines {', '.join(map(str, line_numbers[:5]))}"
            if len(line_numbers) > 5:
                line_info += f" and {len(line_numbers) - 5} more"
            
            return ToolResult(
                error=f"old_str appears {count} times{line_info} — set replace_all=true or provide more context",
                is_error=True,
            )
        
        # Handle dry-run mode
        if dry_run:
            if count == 0:
                if old_str == "" and new_str == "":
                    output_msg = f"Would make no changes to {resolved} (empty-to-empty replacement is a no-op)"
                else:
                    output_msg = f"Would make no changes to {resolved} (old_str not found)"
            else:
                # Format preview output
                preview_lines = []
                preview_lines.append(f"Would replace {count} occurrence(s) in {resolved}:")
                for line_num, old_line, new_line in changes_preview:
                    if old_line == "":
                        preview_lines.append(f"  Line {line_num}: [ADD] '{new_line}'")
                    elif new_line == "":
                        preview_lines.append(f"  Line {line_num}: [REMOVE] '{old_line}'")
                    else:
                        preview_lines.append(f"  Line {line_num}: '{old_line}' -> '{new_line}'")
                
                output_msg = "\n".join(preview_lines)
            
            return ToolResult(
                output=output_msg,
                metadata={"changes_preview": changes_preview}
            )
        
        # Use consolidated atomic validation and write
        write_result = await self.file_security.atomic_validate_and_write(
            config, path, new_text, require_exists=True, check_scope=True, resolve_symlinks=False
        )
        if write_result.is_error:
            return write_result
        
        # Calculate actual number of replacements made
        # For non-dry-run, we already have count from the helper
        # For replace_all=False, we only replace once even if count > 1
        replaced = count if replace_all else min(count, 1)
        
        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {resolved}")
