"""lint_check — run ruff on specific files and return structured diagnostics.

Unlike the cycle_hooks static check (which is a commit gate), this tool
lets the agent proactively lint files during exploration and after edits,
getting structured feedback (file, line, column, rule code, message) that
is easy to act on.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class LintCheckTool(Tool):
    name = "lint_check"
    description = (
        "Run ruff (Python linter) on specific files or directories and return "
        "structured diagnostics. Each diagnostic includes file, line, column, "
        "rule code, and message. Use after editing code to catch issues before "
        "the commit hook. Also supports --fix mode to auto-fix safe issues."
    )
    requires_path_check = True
    tags = frozenset({"analysis"})

    _MAX_OUTPUT_CHARS = 20_000

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files or directories to lint. "
                        "Examples: ['harness/tools/bash.py'] or ['harness/'] for a whole package."
                    ),
                },
                "fix": {
                    "type": "boolean",
                    "description": (
                        "Auto-fix safe issues (ruff --fix). Default: false. "
                        "Only fixes issues that ruff marks as safely fixable."
                    ),
                },
                "select": {
                    "type": "string",
                    "description": (
                        "Comma-separated rule codes to check. "
                        "Examples: 'E,W' for errors+warnings, 'F401' for unused imports, "
                        "'I' for import sorting. If omitted, uses project ruff config."
                    ),
                },
            },
            "required": ["paths"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        paths: list[str],
        fix: bool = False,
        select: str = "",
    ) -> ToolResult:
        if not paths:
            return ToolResult(error="paths must be a non-empty list", is_error=True)

        # Resolve paths relative to workspace
        resolved: list[str] = []
        for p in paths:
            if os.path.isabs(p):
                resolved.append(p)
            else:
                resolved.append(os.path.join(config.workspace, p))

        # Build ruff command
        cmd = ["ruff", "check", "--output-format=json"]
        if fix:
            cmd.append("--fix")
        if select:
            cmd.extend(["--select", select])
        cmd.extend(resolved)

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            return ToolResult(error="ruff timed out after 60s", is_error=True)
        except FileNotFoundError:
            return ToolResult(
                error="ruff is not installed. Install with: pip install ruff",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(error=f"Failed to run ruff: {exc}", is_error=True)

        raw_out = stdout.decode(errors="replace")
        raw_err = stderr.decode(errors="replace")

        # ruff returns exit code 1 when issues found (not an error)
        if proc.returncode not in (0, 1):
            return ToolResult(
                error=f"ruff failed (exit {proc.returncode}): {raw_err.strip()}",
                is_error=True,
            )

        # Parse JSON output
        try:
            diagnostics = json.loads(raw_out) if raw_out.strip() else []
        except json.JSONDecodeError:
            # Fallback: return raw output
            output = f"ruff output (non-JSON):\n{raw_out}"
            if len(output) > self._MAX_OUTPUT_CHARS:
                output = output[: self._MAX_OUTPUT_CHARS] + "\n... [truncated]"
            return ToolResult(output=output)

        if not diagnostics:
            target = ", ".join(os.path.basename(p) for p in paths[:5])
            if len(paths) > 5:
                target += f" (+{len(paths) - 5} more)"
            return ToolResult(output=f"No issues found in {target}")

        # Format structured output
        lines: list[str] = []
        for d in diagnostics:
            filename = d.get("filename", "?")
            # Make path relative to workspace for readability
            if filename.startswith(config.workspace):
                filename = filename[len(config.workspace) :].lstrip("/")
            row = d.get("location", {}).get("row", "?")
            col = d.get("location", {}).get("column", "?")
            code = d.get("code", "?")
            message = d.get("message", "?")
            fix_info = ""
            if d.get("fix") and d["fix"].get("applicability") == "safe":
                fix_info = " [auto-fixable]"
            lines.append(f"  {filename}:{row}:{col}  {code}  {message}{fix_info}")

        header = f"Found {len(diagnostics)} issue(s):\n"
        if fix:
            header = f"Found {len(diagnostics)} issue(s) (--fix applied where safe):\n"
        output = header + "\n".join(lines)

        if len(output) > self._MAX_OUTPUT_CHARS:
            output = output[: self._MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)
