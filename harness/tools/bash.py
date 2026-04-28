"""bash — execute shell commands."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# Shell metacharacters that can chain a second command after a benign-looking
# first token. The old denylist check only inspected the first token of the
# whole command; `echo hi && rm -rf /` bypassed it because `echo` is allowed.
# We split on these operators, then check the leading token of every segment.
# Single `|` is included so `ls | rm -rf /` is caught; `||` is handled by the
# same split since `|` matches sub-string.
_SHELL_CHAIN_RE = re.compile(r"&&|\|\||;|\||&")


class BashTool(Tool):
    name = "bash"
    description = (
        "LAST RESORT — execute a shell command only when no dedicated tool "
        "exists. Runs in the workspace directory; timeout 60s. "
        "Legitimate uses: pip install, git push, make, cargo build, custom "
        "scripts. "
        "WRONG (use the dedicated tool instead): "
        "cat/head/tail/sed → batch_read; "
        "grep → grep_search; "
        "ls/find → list_directory/glob_search; "
        "wc -l → file_info; "
        "pytest → test_runner; "
        "ruff → lint_check. "
        "If you find yourself typing bash, check whether a dedicated tool "
        "can do it — it almost always can."
    )
    tags = frozenset({"execution"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to run in the workspace directory. "
                        "NEVER use to read source files "
                        "(no cat/head/tail/sed/grep on .py files). "
                        "Use for: builds, tests, git, package installs, "
                        "and metadata inspection. "
                        "For reading source files use batch_read; "
                        "for edits use batch_edit or batch_write."
                    ),
                },
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
        """Return the matched denylist entry, if any, or None.

        Checks the first token of **every** shell-chain segment, not just the
        overall command. `echo hi && rm -rf /` splits into ``['echo hi ',
        ' rm -rf /']`` and the `rm` segment is caught. Chaining operators
        handled: ``&&  ||  ;  |  &``.

        Uses shlex for the per-segment tokenisation so quoted leading tokens
        work correctly; falls back to plain split on shlex.ValueError
        (unmatched quote, etc.) — malformed commands are still checked, not
        silently allowed.
        """
        denyset = set(denylist)
        segments = _SHELL_CHAIN_RE.split(command) if command else []
        if not segments:
            return None
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            try:
                tokens = shlex.split(seg)
            except ValueError:
                tokens = seg.split()
            if not tokens:
                continue
            first = tokens[0]
            # Strip any path prefix so "/usr/bin/rm" matches "rm".
            first_base = os.path.basename(first)
            if first in denyset:
                return first
            if first_base in denyset:
                return first_base
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
