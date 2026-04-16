"""read_file — read file contents with optional offset/limit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read the contents of a file. Supports offset (line number to start from, "
        "1-based) and limit (max lines to read) for large files."
    )
    requires_path_check = True
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "offset": {
                    "type": "integer",
                    "description": "Start reading from this line (1-based). Default: 1",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of lines to read. Default: 2000",
                    "default": 2000,
                },
            },
            "required": ["path"],
        }

    def _validate_path_contains_no_homoglyphs(self, path: str) -> str | None:
        """Check if path contains Unicode homoglyphs that could bypass security.
        
        Homoglyphs are characters that look like ASCII but are different code points.
        For example, CYRILLIC SMALL LETTER A (U+0430) looks like ASCII 'a' (U+0061).
        
        Args:
            path: The path string to validate
            
        Returns:
            Error message if homoglyph found, None if path is clean
        """
        # Start with a minimal, high-risk character set
        # These are visual spoofs of ASCII path delimiters or common letters
        homoglyphs = {
            '\u0430': 'Cyrillic small a (looks like ASCII a)',
            '\u04CF': 'Cyrillic small palochka (looks like ASCII l)',
            '\u0500': 'Cyrillic capital komi s (looks like ASCII O)',
            '\u01C3': 'Latin letter retroflex click (looks like ASCII !)',
            '\u0391': 'Greek capital alpha (looks like ASCII A)',
            '\u03B1': 'Greek small alpha (looks like ASCII a)',
            '\u041E': 'Cyrillic capital O (looks like ASCII O)',
            '\u043E': 'Cyrillic small o (looks like ASCII o)',
            '\u0555': 'Armenian comma (looks like ASCII comma)',
            '\u058A': 'Armenian hyphen (looks like ASCII hyphen)',
        }
        
        for char, description in homoglyphs.items():
            if char in path:
                return f"Path contains disallowed Unicode homoglyph: {description} (U+{ord(char):04X})"
        
        return None

    async def execute(
        self, config: HarnessConfig, *, path: str, offset: int = 1, limit: int = 2000
    ) -> ToolResult:
        # The Anthropic API occasionally delivers JSON integers as strings when
        # the LLM emits a quoted value (e.g. offset="2").  Coerce defensively so
        # callers get a clear error instead of a confusing TypeError deep inside
        # arithmetic on line 57.
        try:
            offset = int(offset)
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                error=f"offset and limit must be integers, got offset={offset!r} limit={limit!r}: {exc}",
                is_error=True,
            )

        # Validate path doesn't contain Unicode homoglyphs
        if error_msg := self._validate_path_contains_no_homoglyphs(path):
            return ToolResult(error=error_msg, is_error=True)

        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err

        p = Path(resolved)
        if not p.exists():
            return ToolResult(error=f"File not found: {resolved}", is_error=True)
        if not p.is_file():
            return ToolResult(error=f"Not a file: {resolved}", is_error=True)

        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except Exception as exc:
            return ToolResult(error=str(exc), is_error=True)

        start = max(offset - 1, 0)
        selected = lines[start : start + limit]
        numbered = "".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected)
        )
        total = len(lines)
        header = f"[{p.name}] lines {start+1}-{min(start+limit, total)} of {total}\n"
        return ToolResult(output=header + numbered)
