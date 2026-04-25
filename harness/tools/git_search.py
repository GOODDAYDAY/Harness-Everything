"""git_search — search git history, blame, and picklog for a symbol or pattern.

Modes
-----
* ``log`` (default) — search commit messages with a regex; returns matching
  commits with hash, author, date, and message snippet.
* ``blame`` — annotate a file with the commit that last changed each line
  matching a pattern.  Returns a compact table of (commit, author, line).
* ``grep`` — ``git grep`` across the current working tree (all tracked files),
  optionally scoped to a path; faster than Python-level grep for large repos.
* ``show`` — show the diff introduced by a specific commit hash.
* ``log_file`` — show the commit log for a specific file (``git log -- <path>``).

All modes run git commands via asyncio subprocess in the workspace directory,
honouring the same 30-second timeout as the other git tools.

Usage examples
--------------
Agent usage (via tool call)::

    git_search(pattern="add retry logic")
    git_search(mode="blame", path="harness/loop.py", pattern="max_iterations")
    git_search(mode="grep", pattern="def execute", path="harness/tools")
    git_search(mode="show", commit="a1b2c3d")
    git_search(mode="log_file", path="harness/tools/bash.py")
    git_search(mode="log", pattern="security", limit=20)
"""

from __future__ import annotations

import asyncio
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

_VALID_MODES = ("log", "blame", "grep", "show", "log_file")
_MAX_OUTPUT_CHARS = 20_000


async def _run_git(
    config: HarnessConfig,
    *args: str,
    timeout: int = 30,
) -> tuple[str, str, int]:
    """Run a git command and return (stdout, stderr, returncode)."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.workspace,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        return "", "git command timed out", -1
    except Exception as exc:
        return "", str(exc), -1

    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        proc.returncode,
    )


class GitSearchTool(Tool):
    """Search git history, blame, and grep for patterns.

    Modes
    -----
    * ``log``      — Search commit *messages* with a regex.
    * ``blame``    — Find the commit responsible for lines matching a regex in a file.
    * ``grep``     — Search current working-tree content via ``git grep``.
    * ``show``     — Show the diff introduced by a commit hash.
    * ``log_file`` — Commit history for a specific file.
    """

    name = "git_search"
    description = (
        "Search git history, blame, and tracked files. "
        "mode='log': find commits whose message matches a regex (default). "
        "mode='blame': show which commit last changed lines matching a pattern in a file. "
        "mode='grep': fast git grep across all tracked files. "
        "mode='show': show the diff for a specific commit hash. "
        "mode='log_file': commit history for a specific file path."
    )
    requires_path_check = False  # uses workspace root; no arbitrary path access
    tags = frozenset({"git"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(_VALID_MODES),
                    "description": (
                        "Search mode: 'log' (default) | 'blame' | 'grep' | "
                        "'show' | 'log_file'."
                    ),
                    "default": "log",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Regex/text pattern to search for. "
                        "Required for 'log', 'blame', 'grep'. "
                        "Not used by 'show' or 'log_file'."
                    ),
                    "default": "",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory path (relative to workspace). "
                        "Required for 'blame' and 'log_file'. "
                        "Optional scope restriction for 'grep' and 'log'."
                    ),
                    "default": "",
                },
                "commit": {
                    "type": "string",
                    "description": "Commit hash for 'show' mode.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max results to return (max: 200). Required — no default. "
                        "Use 5-10 for targeted lookup, 20-50 for history scan."
                    ),
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive pattern matching (default: false).",
                    "default": False,
                },
            },
            "required": ["limit"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        mode: str = "log",
        pattern: str = "",
        path: str = "",
        commit: str = "",
        limit: int,
        case_insensitive: bool = False,
    ) -> ToolResult:
        # Validate mode
        if mode not in _VALID_MODES:
            return ToolResult(
                error=(
                    f"Unknown mode {mode!r}. "
                    f"Valid modes: {', '.join(_VALID_MODES)}"
                ),
                is_error=True,
            )

        # Clamp limit
        limit = max(1, min(200, int(limit)))

        if mode == "log":
            return await self._mode_log(config, pattern, path, limit, case_insensitive)
        elif mode == "blame":
            return await self._mode_blame(config, pattern, path, limit, case_insensitive)
        elif mode == "grep":
            return await self._mode_grep(config, pattern, path, limit, case_insensitive)
        elif mode == "show":
            return await self._mode_show(config, commit)
        else:  # log_file
            return await self._mode_log_file(config, path, limit)

    # ------------------------------------------------------------------
    # Mode implementations
    # ------------------------------------------------------------------

    async def _mode_log(
        self,
        config: HarnessConfig,
        pattern: str,
        path: str,
        limit: int,
        case_insensitive: bool,
    ) -> ToolResult:
        """Search commit messages for pattern."""
        if not pattern:
            return ToolResult(
                error="'pattern' is required for mode='log'",
                is_error=True,
            )

        args: list[str] = [
            "log",
            "--oneline",
            "--decorate=no",
            f"--grep={pattern}",
            f"-{limit}",
        ]
        if case_insensitive:
            args.append("-i")
        if path:
            args.extend(["--", path])

        out, err, code = await _run_git(config, *args)
        if code != 0:
            return ToolResult(error=err.strip() or "git log failed", is_error=True)
        if not out.strip():
            return ToolResult(
                output=f"No commits found matching '{pattern}'"
            )

        lines = out.strip().splitlines()
        header = f"Found {len(lines)} commit(s) matching '{pattern}':\n"
        output = header + "\n".join(lines)
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    async def _mode_blame(
        self,
        config: HarnessConfig,
        pattern: str,
        path: str,
        limit: int,
        case_insensitive: bool,
    ) -> ToolResult:
        """Show blame info for lines in a file that match pattern."""
        if not path:
            return ToolResult(
                error="'path' is required for mode='blame'",
                is_error=True,
            )

        # Run git blame with porcelain format for easy parsing
        blame_args = ["blame", "--porcelain", "--", path]
        out, err, code = await _run_git(config, *blame_args)
        if code != 0:
            return ToolResult(error=err.strip() or "git blame failed", is_error=True)

        # Parse porcelain blame: blocks start with "<hash> <orig-line> <final-line>"
        # then metadata lines, then a tab-prefixed source line.
        results = self._parse_blame_porcelain(out, pattern, case_insensitive, limit)
        if not results:
            msg = (
                f"No lines matching '{pattern}' in {path}"
                if pattern
                else f"No blame output for {path}"
            )
            return ToolResult(output=msg)

        header = f"Blame results for '{pattern}' in {path} ({len(results)} lines):\n"
        rows = [
            f"  {r['commit'][:8]}  {r['author']:<20}  line {r['line']:>4}: {r['text']}"
            for r in results
        ]
        output = header + "\n".join(rows)
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    async def _mode_grep(
        self,
        config: HarnessConfig,
        pattern: str,
        path: str,
        limit: int,
        case_insensitive: bool,
    ) -> ToolResult:
        """Run git grep across tracked files."""
        if not pattern:
            return ToolResult(
                error="'pattern' is required for mode='grep'",
                is_error=True,
            )

        args: list[str] = ["grep", "--line-number", "-e", pattern]
        if case_insensitive:
            args.insert(2, "-i")  # before -e
        if path:
            args.extend(["--", path])

        out, err, code = await _run_git(config, *args)
        # git grep returns exit code 1 when no matches found (not an error)
        if code not in (0, 1):
            return ToolResult(error=err.strip() or "git grep failed", is_error=True)
        if not out.strip():
            return ToolResult(output=f"No matches for '{pattern}' in tracked files")

        lines = out.strip().splitlines()[:limit]
        header = f"git grep results for '{pattern}' ({len(lines)} shown):\n"
        output = header + "\n".join(lines)
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    async def _mode_show(
        self,
        config: HarnessConfig,
        commit: str,
    ) -> ToolResult:
        """Show the diff for a specific commit."""
        if not commit:
            return ToolResult(
                error="'commit' is required for mode='show'",
                is_error=True,
            )
        # Sanitize: commit should be a hex hash or short ref, reject anything
        # with shell-special characters to prevent argument injection.
        safe_chars = set("0123456789abcdefABCDEF~^@{}./:-_")
        if not all(c in safe_chars for c in commit):
            return ToolResult(
                error=f"Invalid commit reference {commit!r} — use a hex hash or simple ref",
                is_error=True,
            )

        out, err, code = await _run_git(config, "show", "--stat", commit)
        if code != 0:
            return ToolResult(error=err.strip() or "git show failed", is_error=True)

        output = out
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    async def _mode_log_file(
        self,
        config: HarnessConfig,
        path: str,
        limit: int,
    ) -> ToolResult:
        """Show commit history for a specific file."""
        if not path:
            return ToolResult(
                error="'path' is required for mode='log_file'",
                is_error=True,
            )

        args = [
            "log",
            "--oneline",
            "--decorate=no",
            "--follow",  # follow renames
            f"-{limit}",
            "--",
            path,
        ]
        out, err, code = await _run_git(config, *args)
        if code != 0:
            return ToolResult(error=err.strip() or "git log failed", is_error=True)
        if not out.strip():
            return ToolResult(output=f"No commits found for '{path}'")

        lines = out.strip().splitlines()
        header = f"Commit history for '{path}' ({len(lines)} commits):\n"
        output = header + "\n".join(lines)
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    # ------------------------------------------------------------------
    # Blame porcelain parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_blame_porcelain(
        raw: str,
        pattern: str,
        case_insensitive: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Parse git blame --porcelain output.

        Each blame block has this structure:
            <40-hex-hash> <orig-line> <final-line> [<num-lines>]
            author <name>
            author-mail <email>
            author-time <unix>
            author-tz <tz>
            committer ...
            summary <message>
            [previous <hash> <filename>]
            filename <filepath>
            \t<source line text>

        We collect the hash, author name, line number, and source text.
        """
        import re

        results: list[dict[str, Any]] = []
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags) if pattern else None
        except re.error:
            regex = None

        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            # A blame block starts with a 40-char hex hash followed by line numbers
            if len(line) >= 40 and all(c in "0123456789abcdef" for c in line[:40]):
                parts = line.split()
                commit_hash = parts[0] if parts else "unknown"
                try:
                    final_line = int(parts[2]) if len(parts) >= 3 else 0
                except ValueError:
                    final_line = 0

                # Scan ahead for metadata and the tab-prefixed source line
                author = "unknown"
                source_text = ""
                j = i + 1
                while j < len(lines):
                    meta = lines[j]
                    if meta.startswith("author ") and not meta.startswith("author-"):
                        author = meta[7:].strip()
                    elif meta.startswith("\t"):
                        source_text = meta[1:]  # strip the leading tab
                        i = j  # advance outer loop past this block
                        break
                    j += 1

                # Apply pattern filter
                if regex is None or regex.search(source_text):
                    results.append({
                        "commit": commit_hash,
                        "author": author,
                        "line": final_line,
                        "text": source_text.rstrip(),
                    })
                    if len(results) >= limit:
                        break
            i += 1

        return results
