"""discovery — Tool auto-discovery and runtime introspection.

Two public surfaces
-------------------
1. ``discover_tools(directory)`` — **utility function**: scans a directory for
   Python modules, imports them, and returns every concrete ``Tool`` subclass
   found.  Useful for third-party plugin directories that want to drop a
   ``*.py`` file and have it picked up automatically without editing
   ``__init__.py``.

2. ``ToolDiscoveryTool`` (``name = "tool_discovery"``) — **tool**: lets the
   LLM agent introspect the currently registered tool set at runtime.  Returns
   each tool's name, description, required/optional parameters, and whether it
   requires a path check.  Supports filtering by name substring and listing
   parameter schemas in full.

Design goals
------------
* Zero extra dependencies — pure stdlib (``importlib``, ``inspect``, ``pkgutil``).
* Safe — ``discover_tools`` catches and logs ``ImportError`` / ``Exception``
  per module; a broken plugin never aborts discovery of healthy modules.
* The ``ToolDiscoveryTool`` itself does NOT scan the filesystem (it queries the
  live registry passed via ``config``); scanning is the job of
  ``discover_tools()``.

Usage examples
--------------
Agent usage (via tool call)::

    tool_discovery()
    tool_discovery(filter="search")
    tool_discovery(tool_name="call_graph", show_schema=true)

Python usage (programmatic discovery)::

    from harness.tools.discovery import discover_tools
    plugins = discover_tools("/path/to/plugins")
    for cls in plugins:
        registry.register(cls())
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public utility: discover_tools()
# ---------------------------------------------------------------------------


def discover_tools(
    directory: str | Path,
    *,
    package: str | None = None,
    skip_names: set[str] | None = None,
) -> list[type[Tool]]:
    """Scan *directory* for Python modules and return all concrete Tool subclasses found.

    Each ``*.py`` file in *directory* (non-recursive) is imported as a module.
    All classes defined within it that are:

    - a subclass of ``Tool``
    - **not** ``Tool`` itself
    - **not** abstract (i.e., have no unimplemented abstract methods)

    are collected and returned.

    Args:
        directory: Path to scan for ``*.py`` files.
        package:   Dotted package name to use as the parent when constructing
                   module names (e.g. ``"harness.tools"``).  When ``None``, each
                   module is loaded as a top-level module named after the file
                   stem.  Providing a *package* avoids name collisions when two
                   directories have a file with the same name.
        skip_names: Optional set of module stem names to skip (e.g.
                    ``{"base", "__init__", "registry"}``).  The defaults
                    ``__init__``, ``base``, and ``registry`` are always skipped
                    because they define infrastructure, not tools.

    Returns:
        Deduplicated list of concrete Tool subclass *types* (not instances).
        Order is deterministic: alphabetical by module stem, then by class name.

    Example::

        from harness.tools.discovery import discover_tools
        classes = discover_tools("harness/tools", package="harness.tools")
        tools = [cls() for cls in classes]
    """
    root = Path(os.path.realpath(directory))
    if not root.is_dir():
        log.warning("discover_tools: %s is not a directory — returning empty list", root)
        return []

    # Always skip infrastructure modules that contain no Tool subclasses.
    _always_skip = {"__init__", "base", "registry"}
    effective_skip = _always_skip | (skip_names or set())

    discovered: list[type[Tool]] = []
    seen_classes: set[type] = set()

    # Sort files for deterministic output
    py_files = sorted(root.glob("*.py"), key=lambda p: p.stem)

    for py_file in py_files:
        stem = py_file.stem
        if stem in effective_skip:
            continue

        # Build a fully-qualified module name
        if package:
            module_name = f"{package}.{stem}"
        else:
            module_name = stem

        # Re-use already-imported module to avoid double-loading
        if module_name in sys.modules:
            mod = sys.modules[module_name]
        else:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                log.debug("discover_tools: could not create spec for %s — skipping", py_file)
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod  # register before exec to support intra-package refs
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning(
                    "discover_tools: failed to import %s — %s: %s",
                    module_name, type(exc).__name__, exc,
                )
                del sys.modules[module_name]  # don't leave a broken module cached
                continue

        # Collect Tool subclasses defined in this module
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                obj is Tool
                or not issubclass(obj, Tool)
                or obj in seen_classes
            ):
                continue
            # Skip abstract classes (any unimplemented abstractmethods)
            if inspect.isabstract(obj):
                continue
            seen_classes.add(obj)
            discovered.append(obj)
            log.debug("discover_tools: found %s in %s", obj.__name__, module_name)

    return discovered


# ---------------------------------------------------------------------------
# ToolDiscoveryTool — runtime introspection of the live tool registry
# ---------------------------------------------------------------------------

_MAX_OUTPUT_BYTES = 24_000


class ToolDiscoveryTool(Tool):
    """Introspect the currently available tools in the harness registry.

    Returns a catalogue of registered tools with their names, descriptions,
    required/optional parameters, and schema details.  Useful when the agent
    wants to know what capabilities are available before deciding which tool
    to use.

    Modes
    -----
    * Called with no arguments — returns a compact summary of ALL registered
      tools: name, one-line description, required parameters.
    * ``filter`` — restrict the summary to tools whose name or description
      contains the given substring (case-insensitive).
    * ``tool_name`` — return the full JSON Schema for a single named tool.
    * ``show_schema=true`` — include the full ``input_schema`` for every
      tool in the listing (verbose; useful for exploration).

    The tool reads from ``config`` at call time — it always reflects the live
    registry state, not a static snapshot.
    """

    name = "tool_discovery"
    description = (
        "List and introspect tools available in the current harness registry. "
        "With no args: compact summary of all tools (name + description + required params). "
        "filter='search': restrict to tools matching a substring of name/description. "
        "tool_name='call_graph': full schema for one tool. "
        "show_schema=true: include full input_schema for every tool listed. "
        "Useful for discovering available capabilities before choosing a tool."
    )
    requires_path_check = False  # no filesystem access

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring to filter tools by name or "
                        "description (e.g. 'search', 'git', 'file'). "
                        "Default: '' (no filter — return all tools)."
                    ),
                    "default": "",
                },
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Exact name of a single tool to inspect. "
                        "Returns the full input_schema for that tool. "
                        "Takes precedence over filter/show_schema."
                    ),
                    "default": "",
                },
                "show_schema": {
                    "type": "boolean",
                    "description": (
                        "If true, include the full input_schema for every tool "
                        "in the listing.  Default: false (compact summary only)."
                    ),
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        filter: str = "",  # noqa: A002
        tool_name: str = "",
        show_schema: bool = False,
    ) -> ToolResult:
        # Retrieve the live registry from config.  HarnessConfig stores it as
        # config.tool_registry; fall back gracefully if the attribute is absent
        # (e.g. in unit tests that construct a bare HarnessConfig).
        registry = getattr(config, "tool_registry", None)
        if registry is None:
            # No registry attached — discover from the built-in DEFAULT_TOOLS
            # so the tool still returns useful output.
            from harness.tools import DEFAULT_TOOLS  # local import to avoid circularity
            tools: list[Tool] = list(DEFAULT_TOOLS)
        else:
            tools = [registry.get(n) for n in registry.names]
            tools = [t for t in tools if t is not None]

        # ---- single-tool schema lookup ------------------------------------
        if tool_name:
            match = next((t for t in tools if t.name == tool_name), None)
            if match is None:
                available = sorted(t.name for t in tools)
                return ToolResult(
                    error=(
                        f"Tool {tool_name!r} not found in registry. "
                        f"Available: {available}"
                    ),
                    is_error=True,
                )
            result = {
                "name": match.name,
                "description": match.description,
                "requires_path_check": match.requires_path_check,
                "input_schema": match.input_schema(),
            }
            return ToolResult(output=self._safe_json(result, max_bytes=_MAX_OUTPUT_BYTES))

        # ---- filtered listing --------------------------------------------
        needle = filter.lower().strip()
        if needle:
            tools = [
                t for t in tools
                if needle in t.name.lower() or needle in t.description.lower()
            ]

        # Build compact summaries
        entries: list[dict[str, Any]] = []
        for t in sorted(tools, key=lambda x: x.name):
            entry: dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "requires_path_check": t.requires_path_check,
            }
            # Always include required params for quick reference
            try:
                schema = t.input_schema()
                props = schema.get("properties", {})
                required = schema.get("required", [])
                optional = [k for k in props if k not in required]
                entry["required_params"] = required
                entry["optional_params"] = optional
                if show_schema:
                    entry["input_schema"] = schema
            except Exception:
                entry["required_params"] = []
                entry["optional_params"] = []

            entries.append(entry)

        result_dict: dict[str, Any] = {
            "total_tools": len(tools),
            "filter_applied": needle or None,
            "tools": entries,
        }
        return ToolResult(output=self._safe_json(result_dict, max_bytes=_MAX_OUTPUT_BYTES))
