"""Shared path-extraction helpers for tool call logs.

One source of truth for "which files did this tool call touch" — used by the
LLM's cache-invalidation layer, the executor's files_changed tracking, the
phase runner's commit-message builder, and the agent loop's hook
preparation. Previously each site had its own near-identical enumeration.
"""

from __future__ import annotations

from typing import Any, Iterable


# Tools that take a single ``path`` key.
_SINGLE_PATH_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "file_patch", "find_replace",
    "delete_file",
})


def extract_written_paths(tool_name: str, params: dict[str, Any]) -> list[str]:
    """Return the paths this one tool call will mutate, in declaration order.

    Single-path tools carry a ``path`` key; batch variants carry lists.
    Returns an empty list for non-write tools so callers can iterate uniformly.
    ``move_file`` / ``copy_file`` report the destination (the side that gets
    created); callers that also need the source should enumerate it separately.
    """
    if tool_name in _SINGLE_PATH_TOOLS:
        p = params.get("path")
        return [str(p)] if p else []
    if tool_name in ("move_file", "copy_file"):
        dst = params.get("destination")
        return [str(dst)] if dst else []
    if tool_name == "batch_edit":
        return [
            str(e["path"]) for e in (params.get("edits") or [])
            if isinstance(e, dict) and e.get("path")
        ]
    if tool_name == "batch_write":
        return [
            str(f["path"]) for f in (params.get("files") or [])
            if isinstance(f, dict) and f.get("path")
        ]
    return []


def collect_changed_paths(
    execution_log: Iterable[dict[str, Any]],
    *,
    success_only: bool = True,
) -> list[str]:
    """Dedupe the paths touched across an entire tool-use log.

    Order-preserving: the first time each path appears wins. When
    ``success_only`` is True, entries marked errored are skipped — that's
    the right default for "files I just committed" tracking. For cache
    invalidation, pass ``success_only=False`` so a failed write still
    invalidates the cache entry.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in execution_log:
        if success_only:
            # Entries come from two shapes in the codebase: ones with
            # explicit ``success: bool``, and older ones with ``is_error: bool``.
            # Accept either; default to treating unknown as success.
            if "success" in entry:
                if not entry["success"]:
                    continue
            elif entry.get("is_error"):
                continue
        for p in extract_written_paths(
            entry.get("tool", ""), entry.get("input") or {},
        ):
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)
    return ordered
