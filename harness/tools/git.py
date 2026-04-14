"""git_status / git_diff / git_log — git information tools (read-only)."""

from __future__ import annotations

import asyncio
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


async def _run_git(config: HarnessConfig, *args: str) -> ToolResult:
    """Run a git command in the workspace and return a ToolResult."""
    cmd = ["git", *args]
    # Declare proc before the try so it is always in scope in the except block,
    # even when create_subprocess_exec itself raises.
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.workspace,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        # Kill the child process and reap it so the OS does not keep a
        # zombie entry and asyncio does not warn about an unclosed transport.
        if proc is not None:
            proc.kill()
            await proc.wait()
        return ToolResult(error="git command timed out", is_error=True)
    except Exception as exc:
        return ToolResult(error=str(exc), is_error=True)

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if proc.returncode != 0:
        return ToolResult(error=err or out, is_error=True)
    return ToolResult(output=out or "(empty)")


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the working tree status (git status)."

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, config: HarnessConfig) -> ToolResult:
        return await _run_git(config, "status", "--short")


class GitDiffTool(Tool):
    name = "git_diff"
    description = (
        "Show file changes. By default shows unstaged changes. "
        "Set staged=true to see staged changes."
    )

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes instead (default: false)",
                    "default": False,
                },
                "path": {
                    "type": "string",
                    "description": "Limit diff to a specific file/directory",
                    "default": "",
                },
            },
        }

    async def execute(
        self, config: HarnessConfig, *, staged: bool = False, path: str = ""
    ) -> ToolResult:
        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args.extend(["--", path])
        return await _run_git(config, *args)


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent commit log."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of commits to show (default: 10)",
                    "default": 10,
                },
                "oneline": {
                    "type": "boolean",
                    "description": "One-line format (default: true)",
                    "default": True,
                },
            },
        }

    async def execute(
        self, config: HarnessConfig, *, count: int = 10, oneline: bool = True
    ) -> ToolResult:
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        return await _run_git(config, *args)
