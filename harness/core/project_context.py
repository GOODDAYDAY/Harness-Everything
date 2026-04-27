"""project_context — lightweight project-structure snapshot.

Injects a compact, signal-dense block of project metadata so the LLM can
reason about *what is already there* before deciding what to change.

What is collected
-----------------
1. **Directory tree** (depth-limited, hidden dirs excluded) — a map of the
   project layout so real file paths can be referenced.
2. **Recent git log** (last N commits, one-line) — shows what has changed
   recently so the agent doesn't re-do work or conflict with the latest state.
3. **Git status** — current working-tree changes (M/A/D/?) so the agent
   knows what is already modified.
4. **Key file inventory** — glob-based listing of Python files, test files,
   and config files.

All collection is best-effort: if git is absent or the workspace has no git
repo, the git sections are silently omitted.  If a glob finds nothing, it is
omitted too.  The result is always a valid (possibly sparse) string.
"""

from __future__ import annotations

import asyncio
import glob as glob_mod
import logging
from pathlib import Path

from harness.core.config import HarnessConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

_TREE_MAX_DEPTH: int = 3        # levels to recurse in directory tree
_TREE_MAX_ENTRIES: int = 150    # hard cap on total entries shown in tree
_GIT_LOG_COUNT: int = 12        # recent commits to show
_FILE_GLOB_LIMIT: int = 40      # max files per glob category
_MAX_OUTPUT_CHARS: int = 6_000  # total cap on the formatted block

# Directories to skip in the tree (noise / not useful to the LLM)
_TREE_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", ".env",
    "dist", "build", ".eggs", "*.egg-info",
    ".tox", ".nox", "htmlcov", ".coverage",
})

# Glob patterns that identify "interesting" file categories to inventory
_FILE_CATEGORIES: list[tuple[str, str]] = [
    ("Python sources",  "**/*.py"),
    ("Tests",           "**/test_*.py"),
    ("Config files",    "*.{json,yaml,yml,toml,ini,cfg}"),
    ("Docs / markdown", "**/*.md"),
]


# ---------------------------------------------------------------------------
# Async subprocess helper
# ---------------------------------------------------------------------------


async def _run_cmd(args: list[str], cwd: str, timeout: int = 10) -> str:
    """Run a subprocess and return stdout as a string, or ``""`` on failure."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return ""
        return stdout.decode(errors="replace")
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Directory tree builder (synchronous — no I/O bottleneck at this depth)
# ---------------------------------------------------------------------------


def _build_tree(
    root: Path,
    prefix: str = "",
    depth: int = 0,
    max_depth: int = _TREE_MAX_DEPTH,
    counter: list[int] | None = None,
) -> list[str]:
    """Recursively build a tree listing, skipping noise directories."""
    if counter is None:
        counter = [0]
    lines: list[str] = []
    if depth >= max_depth:
        return lines

    try:
        entries = sorted(root.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    except PermissionError:
        return lines

    # Filter to visible entries first so connector logic ("is this the last
    # entry?") is based only on what will actually be displayed.  Using the
    # raw enumerate index against the unfiltered list was a bug: a hidden file
    # or noise dir near the end of the sorted list caused the last *visible*
    # entry to receive "├── " (more items follow) instead of "└── " (last item).
    visible = [
        e for e in entries
        if not e.name.startswith(".")
        and e.name not in _TREE_SKIP_DIRS
        and not e.name.endswith(".egg-info")
    ]

    for i, entry in enumerate(visible):
        if counter[0] >= _TREE_MAX_ENTRIES:
            lines.append(f"{prefix}... (truncated)")
            break
        name = entry.name
        is_last = i == len(visible) - 1
        connector = "└── " if is_last else "├── "
        if entry.is_dir():
            lines.append(f"{prefix}{connector}{name}/")
            counter[0] += 1
            extension = "    " if is_last else "│   "
            lines.extend(
                _build_tree(entry, prefix + extension, depth + 1, max_depth, counter)
            )
        elif entry.is_file():
            lines.append(f"{prefix}{connector}{name}")
            counter[0] += 1

    return lines


# ---------------------------------------------------------------------------
# File inventory
# ---------------------------------------------------------------------------


def _file_inventory(workspace: str) -> list[str]:
    """Return compact bullet lines listing key file categories."""
    lines: list[str] = []
    seen: set[str] = set()

    for label, pattern in _FILE_CATEGORIES:
        matches: list[str] = []
        for path_str in glob_mod.glob(pattern, recursive=True, root_dir=workspace):
            full = Path(workspace) / path_str
            resolved = str(full.resolve())
            if resolved in seen or not full.is_file():
                continue
            seen.add(resolved)
            matches.append(path_str)
            if len(matches) >= _FILE_GLOB_LIMIT:
                break

        if not matches:
            continue
        # Sort for determinism
        matches.sort()
        suffix = f" (+{len(matches) - _FILE_GLOB_LIMIT} more)" if len(matches) >= _FILE_GLOB_LIMIT else ""
        lines.append(f"**{label}** ({len(matches)}{suffix}):")
        for m in matches[:_FILE_GLOB_LIMIT]:
            lines.append(f"  • {m}")

    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ProjectContextBuilder:
    """Collect and format project metadata for the Planner.

    Usage::

        builder = ProjectContextBuilder(config)
        ctx_block = await builder.build()
        # prepend ctx_block to Planner.plan(task, context=ctx_block + feedback)
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self._workspace = config.workspace

    async def build(self) -> str:
        """Return a compact, LLM-friendly project context block.

        Collects directory tree, git log, git status, and file inventory
        in parallel, then formats them into a single markdown-ish block.
        Returns ``""`` if nothing meaningful could be collected.
        """
        # Fire all async tasks in parallel.
        # Use get_running_loop() (not the deprecated get_event_loop()) so that
        # run_in_executor always targets the loop that is *currently executing*
        # this coroutine.  get_event_loop() can return a different loop object
        # in Python >= 3.10 when a loop is already running, causing executor
        # tasks to be submitted to the wrong loop and triggering RuntimeErrors.
        loop = asyncio.get_running_loop()
        tree_task = loop.run_in_executor(None, self._sync_tree)
        git_log_task = _run_cmd(
            ["git", "log", f"-{_GIT_LOG_COUNT}", "--oneline", "--no-merges"],
            cwd=self._workspace,
        )
        git_status_task = _run_cmd(
            ["git", "status", "--short"],
            cwd=self._workspace,
        )
        inventory_task = loop.run_in_executor(
            None, lambda: _file_inventory(self._workspace)
        )

        tree_lines, git_log, git_status, inventory_lines = await asyncio.gather(
            tree_task, git_log_task, git_status_task, inventory_task
        )

        parts: list[str] = []

        # High-priority sections first so they survive truncation.
        # Git sections tell the planner *what just changed* — the most
        # actionable signal.  File inventory is useful but expendable.

        # --- git log (highest priority — most actionable recent history) ---
        if git_log.strip():
            parts.append("### Recent Commits (newest first)")
            parts.append("```")
            # Limit to _GIT_LOG_COUNT lines; git log already caps it
            parts.append(git_log.strip())
            parts.append("```")

        # --- git status ---
        if git_status.strip():
            parts.append("\n### Working Tree Status")
            parts.append("```")
            parts.append(git_status.strip())
            parts.append("```")

        # --- directory tree ---
        if tree_lines:
            root_name = Path(self._workspace).name or "workspace"
            parts.append("\n### Project Structure")
            parts.append(f"```\n{root_name}/")
            parts.extend(tree_lines)
            parts.append("```")

        # --- file inventory (lowest priority — truncated first) ---
        if inventory_lines:
            parts.append("\n### File Inventory")
            parts.extend(inventory_lines)

        if not parts:
            return ""

        header = "## Project Context\n\n"
        body = "\n".join(parts)

        # Hard cap to avoid flooding the prompt
        if len(body) > _MAX_OUTPUT_CHARS:
            body = body[:_MAX_OUTPUT_CHARS] + "\n... [project context truncated]"

        result = header + body
        log.debug(
            "ProjectContextBuilder: built %d-char context block", len(result)
        )
        return result

    def _sync_tree(self) -> list[str]:
        """Build directory tree synchronously (called in executor)."""
        root = Path(self._workspace)
        if not root.is_dir():
            return []
        return _build_tree(root, max_depth=_TREE_MAX_DEPTH)
