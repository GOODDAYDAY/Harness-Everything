"""dependency_analyzer — Python import dependency graph and circular import detector.

Parses all Python files under a directory using the AST module to extract
``import`` and ``from … import`` statements.  From those raw imports it builds
a module-level dependency graph and runs a depth-first search to detect
circular import cycles.

Key design decisions
--------------------
* **AST-only parsing** — no ``importlib`` or runtime imports, so the tool
  works safely on arbitrary codebases without executing any code.
* **Module-name normalisation** — absolute and relative imports are both
  converted to dotted module names.  Relative imports are resolved relative
  to the file's own package prefix so "from .utils import X" in
  ``harness/tools/foo.py`` becomes ``harness.tools.utils``.
* **Workspace-local filtering** — only modules whose dotted prefix matches a
  module found in the workspace are included in the dependency graph by
  default (``include_stdlib=False``).  This keeps the graph focused on
  project code and avoids polluting output with hundreds of stdlib edges.
* **Cycle detection** — iterative DFS with a colouring scheme (WHITE /
  GRAY / BLACK) to detect back-edges without recursion-limit issues on
  large graphs.
* **Output budget** — ``_safe_json`` with 24 KB cap.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools._ast_utils import parse_module
from harness.tools.base import Tool, ToolResult

_MAX_OUTPUT_BYTES = 24_000

# DFS colouring constants
_WHITE = 0  # not yet visited
_GRAY = 1   # currently in the DFS stack (potential cycle member)
_BLACK = 2  # fully processed


# ---------------------------------------------------------------------------
# Import extraction helpers
# ---------------------------------------------------------------------------

def _file_to_module(fpath: Path, root: Path) -> str:
    """Convert a file path to a dotted module name relative to *root*.

    ``root/harness/tools/foo.py`` → ``harness.tools.foo``
    ``root/harness/__init__.py`` → ``harness``
    """
    try:
        rel = fpath.relative_to(root)
    except ValueError:
        rel = fpath

    parts = list(rel.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else ""


def _resolve_relative(
    module: str,
    level: int,
    file_module: str,
) -> str:
    """Resolve a relative import to an absolute module name.

    ``level=1`` means ``from . import x``  (same package).
    ``level=2`` means ``from .. import x`` (parent package).

    If the resolution would walk above the root (too many dots) the original
    dotted-module string is returned unchanged as a best-effort fallback.
    """
    parts = file_module.split(".") if file_module else []
    # Strip *level* trailing components (1 = current package)
    if level > len(parts):
        # Can't resolve — return as-is
        return module or file_module
    base_parts = parts[: max(0, len(parts) - level)]
    if module:
        return ".".join(base_parts + [module]) if base_parts else module
    return ".".join(base_parts) if base_parts else ""


def _extract_imports(
    tree: ast.AST,
    file_module: str,
) -> list[str]:
    """Return the list of absolute module names imported by a single AST tree.

    Handles:
    * ``import os``                      → ``["os"]``
    * ``import os.path``                 → ``["os.path"]``
    * ``from pathlib import Path``       → ``["pathlib"]``
    * ``from harness.tools import base`` → ``["harness.tools"]``
    * ``from . import utils``            → ``["<pkg>.utils"]``
    * ``from ..config import X``         → ``["<parent>.config"]``

    Only the top-level *module* is recorded (not the specific name imported),
    which matches Python's actual import semantics where ``from foo import bar``
    loads the ``foo`` module.
    """
    result: list[str] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if mod and mod not in seen:
                    seen.add(mod)
                    result.append(mod)

        elif isinstance(node, ast.ImportFrom):
            raw_module = node.module or ""
            level = node.level or 0
            if level > 0:
                mod = _resolve_relative(raw_module, level, file_module)
            else:
                mod = raw_module
            if mod and mod not in seen:
                seen.add(mod)
                result.append(mod)

    return result


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph(
    py_files: list[Path],
    root: Path,
    include_stdlib: bool,
    known_modules: set[str],
) -> dict[str, list[str]]:
    """Build a module-level dependency graph.

    Returns:
        ``graph[module_name]`` = sorted list of imported module names
        (filtered by workspace membership unless ``include_stdlib=True``).
    """
    graph: dict[str, list[str]] = {}

    for fpath in py_files:
        tree = parse_module(fpath)
        if tree is None:
            continue

        file_mod = _file_to_module(fpath, root)
        if not file_mod:
            continue

        raw_imports = _extract_imports(tree, file_mod)

        # Filter to workspace-local modules if include_stdlib is False
        if include_stdlib:
            filtered = raw_imports
        else:
            filtered = [
                imp for imp in raw_imports
                if _is_workspace_import(imp, known_modules)
            ]

        # Deduplicate while preserving insertion order
        seen: set[str] = set()
        unique: list[str] = []
        for imp in filtered:
            top = imp  # keep full dotted name
            if top not in seen:
                seen.add(top)
                unique.append(top)

        graph[file_mod] = sorted(unique)

    return graph


def _is_workspace_import(imp: str, known_modules: set[str]) -> bool:
    """Return True if *imp* has a prefix matching a known workspace module.

    ``imp = "harness.tools.base"`` matches ``"harness"`` in *known_modules*.
    """
    parts = imp.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in known_modules:
            return True
    return False


def _collect_known_modules(py_files: list[Path], root: Path) -> set[str]:
    """Derive top-level package names from the workspace file list."""
    known: set[str] = set()
    for fpath in py_files:
        mod = _file_to_module(fpath, root)
        if mod:
            top = mod.split(".")[0]
            known.add(top)
            known.add(mod)
    return known


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect all simple cycles in the dependency graph using iterative DFS.

    Returns a list of cycles; each cycle is a list of module names forming
    a loop (last element imports the first to close the cycle).

    Uses the WHITE/GRAY/BLACK colouring scheme to find back-edges:
    - WHITE: not yet visited.
    - GRAY: currently on the DFS stack (back-edge to a GRAY node = cycle).
    - BLACK: fully explored (no further back-edges possible from descendants).

    Cap: stops after 20 cycles to bound output size.
    """
    color: dict[str, int] = defaultdict(lambda: _WHITE)
    cycles: list[list[str]] = []
    MAX_CYCLES = 20

    all_nodes = set(graph.keys())
    for deps in graph.values():
        all_nodes.update(deps)

    for start in sorted(all_nodes):
        if color[start] != _WHITE:
            continue
        if len(cycles) >= MAX_CYCLES:
            break

        # Iterative DFS: stack holds (node, iterator_over_children, path_so_far)
        stack: list[tuple[str, Any, list[str]]] = []
        children_iter = iter(graph.get(start, []))
        path = [start]
        color[start] = _GRAY
        stack.append((start, children_iter, path))

        while stack and len(cycles) < MAX_CYCLES:
            node, children, current_path = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                # Backtrack
                color[node] = _BLACK
                stack.pop()
                continue

            if color[child] == _GRAY:
                # Back-edge found: extract the cycle
                cycle_start_idx = next(
                    (i for i, n in enumerate(current_path) if n == child), None
                )
                if cycle_start_idx is not None:
                    cycle = current_path[cycle_start_idx:] + [child]
                    if cycle not in cycles:
                        cycles.append(cycle)

            elif color[child] == _WHITE:
                color[child] = _GRAY
                new_path = current_path + [child]
                stack.append((child, iter(graph.get(child, [])), new_path))

    return cycles


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class DependencyAnalyzerTool(Tool):
    """Analyze Python import dependencies and detect circular imports."""

    name = "dependency_analyzer"
    description = (
        "Analyze Python import dependencies across a codebase. "
        "Extracts all import statements using AST parsing (no code execution), "
        "builds a module dependency graph, and detects circular imports. "
        "Modes: 'graph' returns the full dependency graph; 'cycles' returns "
        "only circular import chains; 'imports' returns per-file import lists. "
        "By default only workspace-local imports are included (stdlib excluded). "
        "No external dependencies — pure AST."
    )
    requires_path_check = False  # manual allowed_paths enforcement via _check_dir_root
    tags = frozenset({"analysis"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Directory to analyze (default: config.workspace).",
                    "default": "",
                },
                "mode": {
                    "type": "string",
                    "enum": ["graph", "cycles", "imports"],
                    "description": (
                        "Output mode: "
                        "'graph' = full module dependency graph (module → [deps]); "
                        "'cycles' = only circular import chains; "
                        "'imports' = per-file import lists. "
                        "Default: 'graph'."
                    ),
                    "default": "graph",
                },
                "include_stdlib": {
                    "type": "boolean",
                    "description": (
                        "Include standard-library and third-party imports in the "
                        "graph. Default false keeps output focused on project code."
                    ),
                    "default": False,
                },
                "module_filter": {
                    "type": "string",
                    "description": (
                        "Dotted module prefix to restrict output to "
                        "(e.g. 'harness.tools'). Empty string = no filter."
                    ),
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: HarnessConfig,
        root: str = "",
        mode: str = "graph",
        include_stdlib: bool = False,
        module_filter: str = "",
    ) -> ToolResult:
        if mode not in ("graph", "cycles", "imports"):
            return ToolResult(
                error=f"Unknown mode {mode!r}; use graph/cycles/imports",
                is_error=True,
            )

        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        # Collect all known workspace modules for filtering
        known_modules = _collect_known_modules(py_files, search_root)

        # Build the dependency graph
        graph = _build_graph(py_files, search_root, include_stdlib, known_modules)

        # Apply module_filter if requested
        if module_filter:
            graph = {
                mod: deps
                for mod, deps in graph.items()
                if mod.startswith(module_filter)
            }

        if mode == "cycles":
            cycles = _find_cycles(graph)
            output_obj: dict[str, Any] = {
                "root": str(search_root),
                "mode": "cycles",
                "cycles_found": len(cycles),
                "cycles": cycles,
                "modules_analyzed": len(graph),
            }

        elif mode == "imports":
            # Per-file listing: map file path → list of raw imports (pre-filter)
            imports_per_file: list[dict[str, Any]] = []
            for fpath in py_files:
                file_mod = _file_to_module(fpath, search_root)
                if not file_mod:
                    continue
                if module_filter and not file_mod.startswith(module_filter):
                    continue
                tree = parse_module(fpath)
                if tree is None:
                    continue
                raw = _extract_imports(tree, file_mod)
                if not include_stdlib:
                    raw = [i for i in raw if _is_workspace_import(i, known_modules)]
                if raw:
                    try:
                        rel = str(fpath.relative_to(search_root))
                    except ValueError:
                        rel = str(fpath)
                    imports_per_file.append({
                        "file": rel,
                        "module": file_mod,
                        "imports": raw,
                    })

            output_obj = {
                "root": str(search_root),
                "mode": "imports",
                "files_with_imports": len(imports_per_file),
                "files_scanned": len(py_files),
                "imports": imports_per_file,
            }

        else:  # mode == "graph"
            cycles = _find_cycles(graph)
            output_obj = {
                "root": str(search_root),
                "mode": "graph",
                "modules_total": len(graph),
                "files_scanned": len(py_files),
                "cycles_found": len(cycles),
                "cycles": cycles,
                "graph": graph,
            }

        return ToolResult(
            output=self._safe_json(output_obj, max_bytes=_MAX_OUTPUT_BYTES)
        )
