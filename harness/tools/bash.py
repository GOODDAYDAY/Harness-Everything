"""bash — execute shell commands."""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command and return its stdout/stderr. "
        "The command runs in the workspace directory. "
        "Timeout defaults to 60 seconds."
    )

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 60)",
                    "default": 60,
                },
            },
            "required": ["command"],
        }

    @staticmethod
    def _denied_command(command: str, denylist: list[str]) -> str | None:
        """Return the matched denylist entry if the command's first token is denied, else None.

        Splits the command with shlex to handle quoted leading tokens correctly.
        Falls back to a simple split on shlex.ValueError (e.g. unmatched quotes)
        so that malformed commands are still checked rather than silently allowed.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        if not tokens:
            return None
        first = tokens[0]
        # Also strip any path prefix so "/usr/bin/rm" matches "rm"
        first_base = os.path.basename(first)
        for denied in denylist:
            if first == denied or first_base == denied:
                return denied
        return None

    async def execute(
        self, config: HarnessConfig, *, command: str, timeout: int = 60
    ) -> ToolResult:
        # Reject commands whose leading token appears in the denylist.
        if config.bash_command_denylist:
            matched = self._denied_command(command, config.bash_command_denylist)
            if matched is not None:
                return ToolResult(
                    error=(
                        f"PERMISSION ERROR: command {command!r} is blocked "
                        f"(matched denylist entry {matched!r}).  "
                        f"Denylist: {config.bash_command_denylist}"
                    ),
                    is_error=True,
                )

        # Declare proc before the try so it is always in scope in the except
        # block, even if create_subprocess_shell itself raises.
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # Kill the child process and reap it so the OS does not keep a
            # zombie entry and asyncio does not warn about an unclosed transport.
            if proc is not None:
                proc.kill()
                await proc.wait()
            return ToolResult(error=f"Command timed out after {timeout}s", is_error=True)
        except Exception as exc:
            return ToolResult(error=str(exc), is_error=True)

        out = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        code = proc.returncode

        parts: list[str] = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        parts.append(f"[exit code: {code}]")

        text = "\n".join(parts)
        return ToolResult(output=text, is_error=code != 0, error=text if code != 0 else "")
