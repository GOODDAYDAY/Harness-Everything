"""call_graph — AST-based call graph builder.

Given a starting function name, traces all functions it calls and, recursively,
all functions *those* call, up to a configurable depth (default 3, max 5).

The output is a directed graph represented as a dict of nodes.  Each node
entry records:

* ``file`` / ``line`` — definition location (if found in the workspace).
* ``calls`` — list of callee names (functions called *from* this function).
* ``depth`` — distance from the seed function in the call graph.

Key design decisions
--------------------
* **Single AST pass per file** — all files are parsed once; the extracted
  call-edge data is reused across every BFS level, keeping total complexity
  O(depth × files × AST-nodes) rather than O(depth² × …).
* **BFS expansion** — breadth-first traversal ensures the lowest-depth
  assignment wins for cycles (same as shortest-path semantics).
* **Cycle guard** — visited set prevents infinite loops on mutually
  recursive call graphs.
* **Node cap** — stops expansion after 200 unique nodes to prevent
  runaway output on large codebases.
* **Depth cap** — hard-capped at 5 regardless of the ``depth`` parameter.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

_MAX_OUTPUT_BYTES = 24_000
_MAX_NODES = 200
_MAX_DEPTH = 5


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _callee_name(call_node: ast.Call) -> str | None:
    """Extract a human-readable callee name from a Call node.

    Handles:
    * ``foo()``            → ``"foo"``
    * ``obj.foo()``        → ``"obj.foo"``
    * ``a.b.foo()``        → ``"b.foo"``  (attribute chain; keep last two)
    * ``foo()()``          → ``None``     (dynamic; skip)
    """
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        attr = func.attr
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{attr}"
        # Nested attribute like a.b.c → keep b.c for readability
        if isinstance(func.value, ast.Attribute):
            return f"{func.value.attr}.{attr}"
        return attr
    return None


def _extract_function_calls(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    cap: int = 50,
) -> list[str]:
    """Return unique callee names from the body of a function node.

    Capped at *cap* entries to bound per-node output size.
    """
    seen: set[str] = set()
    result: list[str] = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name and name not in seen:
                seen.add(name)
                result.append(name)
                if len(result) >= cap:
                    break
    return result


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def _build_index(
    py_files: list[Path],
    root: Path,
) -> tuple[
    dict[str, dict[str, Any]],          # name → {file, line, calls}
    dict[str, list[dict[str, Any]]],    # bare_name → [{file, line, calls}]
]:
    """Parse all Python files and build two indexes.

    Returns:
        ``defs_by_qualname``: maps ``"ClassName.method"`` and ``"func_name"``
            to a definition record ``{file, line, calls}``.  When the same
            bare name appears in multiple files the *first* encountered wins
            (stable across runs since _rglob_safe returns files in a
            consistent order).
        ``defs_by_bare``: maps the bare function name to a list of all
            definition records (preserves all overloads / same-name-different-
            file occurrences for callee resolution).
    """
    defs_by_qualname: dict[str, dict[str, Any]] = {}
    defs_by_bare: dict[str, list[dict[str, Any]]] = {}

    for fpath in py_files:
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(fpath))
        except (SyntaxError, OSError):
            continue

        try:
            rel = str(fpath.relative_to(root))
        except ValueError:
            rel = str(fpath)

        # Walk top-level and class-body functions
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            bare_name = node.name
            calls = _extract_function_calls(node)
            rec = {"file": rel, "line": node.lineno, "calls": calls}

            # Register bare name (first definition wins for qualname index)
            if bare_name not in defs_by_qualname:
                defs_by_qualname[bare_name] = rec
            defs_by_bare.setdefault(bare_name, []).append(rec)

            # Also register qualified name for method lookups (ClassName.method)
            # We need to find the parent class of this function node.
            # Build a lightweight parent scan inline: walk the tree once per
            # file would be O(n²) total; instead we rely on the outer ast.walk
            # which visits ClassDef nodes before their method children, so
            # we track the most-recently-entered ClassDef via a stack built
            # before we iterate function nodes (see _build_index_for_file).
            # This helper only registers bare names; qualified names are added
            # by _build_index_for_file which has the class context.

    # Second pass: register qualified names (ClassName.method)
    for fpath in py_files:
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(fpath))
        except (SyntaxError, OSError):
            continue

        try:
            rel = str(fpath.relative_to(root))
        except ValueError:
            rel = str(fpath)

        for cls_node in ast.walk(tree):
            if not isinstance(cls_node, ast.ClassDef):
                continue
            for item in ast.iter_child_nodes(cls_node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = f"{cls_node.name}.{item.name}"
                    if qualname not in defs_by_qualname:
                        calls = _extract_function_calls(item)
                        defs_by_qualname[qualname] = {
                            "file": rel,
                            "line": item.lineno,
                            "calls": calls,
                        }

    return defs_by_qualname, defs_by_bare


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class CallGraphTool(Tool):
    """Build a call graph rooted at a given function using AST analysis."""

    name = "call_graph"
    description = (
        "Build a call graph rooted at a given Python function. "
        "Uses AST analysis to trace which functions are called, then "
        "recursively traces those callees up to the specified depth (default 3, max 5). "
        "Returns a graph of nodes, each with its definition location and its "
        "outgoing calls. Supports bare function names ('my_func') and "
        "qualified method names ('MyClass.my_method'). "
        "No external dependencies — pure AST."
    )
    requires_path_check = False  # manual allowed_paths enforcement via _check_dir_root
    tags = frozenset({"analysis"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "function_name": {
                    "type": "string",
                    "description": (
                        "Root function to start the call graph from. "
                        "Use 'my_func' for a bare function or "
                        "'MyClass.my_method' for a method."
                    ),
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search (default: config.workspace).",
                    "default": "",
                },
                "depth": {
                    "type": "integer",
                    "description": "How many call levels deep to trace (default: 3, max: 5).",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 5,
                },
                "include_builtins": {
                    "type": "boolean",
                    "description": (
                        "Include Python built-ins and stdlib names in the graph "
                        "(e.g. 'len', 'print', 'os.path.join'). "
                        "Default false keeps the graph focused on project code."
                    ),
                    "default": False,
                },
            },
            "required": ["function_name"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        function_name: str = "",
        root: str = "",
        depth: int = 3,
        include_builtins: bool = False,
    ) -> ToolResult:
        function_name = function_name.strip()
        if not function_name:
            return ToolResult(error="'function_name' is required", is_error=True)

        # Clamp depth
        depth = max(1, min(_MAX_DEPTH, depth))

        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        # Build definition index from all files
        defs_by_qualname, defs_by_bare = _build_index(py_files, search_root)

        # BFS call graph expansion
        # graph: node_name → {file, line, calls, depth}
        graph: dict[str, dict[str, Any]] = {}
        queue: deque[tuple[str, int]] = deque()
        queue.append((function_name, 0))
        visited: set[str] = set()

        while queue and len(graph) < _MAX_NODES:
            name, current_depth = queue.popleft()
            if name in visited:
                continue
            visited.add(name)

            # Resolve definition
            defn = defs_by_qualname.get(name)
            if defn is None:
                # Try bare name (strips obj. prefix from "obj.method")
                bare = name.split(".")[-1]
                candidates = defs_by_bare.get(bare)
                if candidates:
                    defn = candidates[0]

            if defn is not None:
                calls = defn["calls"]
                node_rec: dict[str, Any] = {
                    "file": defn["file"],
                    "line": defn["line"],
                    "calls": calls,
                    "depth": current_depth,
                    "found": True,
                }
            else:
                # Symbol not found in workspace (builtin, external, etc.)
                node_rec = {
                    "file": None,
                    "line": None,
                    "calls": [],
                    "depth": current_depth,
                    "found": False,
                }
                calls = []

            # Filter builtins if requested
            if not include_builtins and not defn:
                # Only add to graph if it's the root or was found in workspace
                if current_depth > 0:
                    # Still record it in the graph as an external/unresolved ref
                    graph[name] = node_rec
                    continue
            graph[name] = node_rec

            # Enqueue callees if depth budget remains
            if current_depth < depth:
                for callee in calls:
                    if callee not in visited and len(graph) < _MAX_NODES:
                        # Skip obvious builtins/stdlib when include_builtins=False
                        if not include_builtins and _is_likely_builtin(callee):
                            continue
                        queue.append((callee, current_depth + 1))

        # Build summary statistics
        found_count = sum(1 for n in graph.values() if n["found"])
        external_count = sum(1 for n in graph.values() if not n["found"])

        output_obj: dict[str, Any] = {
            "root_function": function_name,
            "depth": depth,
            "nodes_total": len(graph),
            "nodes_found": found_count,
            "nodes_external": external_count,
            "truncated": len(graph) >= _MAX_NODES,
            "graph": graph,
        }

        return ToolResult(output=self._safe_json(output_obj, max_bytes=_MAX_OUTPUT_BYTES))


# ---------------------------------------------------------------------------
# Builtin / stdlib heuristic filter
# ---------------------------------------------------------------------------

# Common Python built-ins and short stdlib names that are almost never
# defined in the project workspace.  This is a best-effort heuristic; the
# full set of built-ins is not enumerated here to keep the list maintainable.
_BUILTIN_NAMES: frozenset[str] = frozenset({
    # Built-in functions
    "abs", "all", "any", "ascii", "bin", "bool", "breakpoint", "bytearray",
    "bytes", "callable", "chr", "classmethod", "compile", "complex",
    "delattr", "dict", "dir", "divmod", "enumerate", "eval", "exec",
    "filter", "float", "format", "frozenset", "getattr", "globals",
    "hasattr", "hash", "help", "hex", "id", "input", "int", "isinstance",
    "issubclass", "iter", "len", "list", "locals", "map", "max", "memoryview",
    "min", "next", "object", "oct", "open", "ord", "pow", "print", "property",
    "range", "repr", "reversed", "round", "set", "setattr", "slice",
    "sorted", "staticmethod", "str", "sum", "super", "tuple", "type",
    "vars", "zip",
    # Common stdlib short names used as callables
    "append", "extend", "update", "get", "keys", "values", "items",
    "join", "split", "strip", "replace", "format", "encode", "decode",
    "read", "write", "close", "seek", "tell",
    "log", "info", "debug", "warning", "error", "exception",
    # Dunder methods (commonly called indirectly via syntax)
    "__init__", "__new__", "__call__", "__repr__", "__str__",
    "__enter__", "__exit__", "__iter__", "__next__",
})

# Prefixes that reliably indicate external/stdlib modules
_BUILTIN_PREFIXES: tuple[str, ...] = (
    "os.", "sys.", "re.", "json.", "ast.", "math.", "io.",
    "logging.", "pathlib.", "shutil.", "subprocess.", "threading.",
    "asyncio.", "typing.", "dataclasses.", "contextlib.", "functools.",
    "itertools.", "collections.", "datetime.", "time.", "random.",
    "hashlib.", "base64.", "urllib.", "http.", "email.",
    "unittest.", "pytest.", "mock.",
)


def _is_likely_builtin(name: str) -> bool:
    """Return True if *name* looks like a Python builtin or stdlib call."""
    bare = name.split(".")[-1]
    if bare in _BUILTIN_NAMES:
        return True
    for prefix in _BUILTIN_PREFIXES:
        if name.startswith(prefix):
            return True
    return False
