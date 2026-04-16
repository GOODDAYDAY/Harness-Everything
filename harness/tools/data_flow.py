"""data_flow — attribute-read and call-chain tracer."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

_MAX_OUTPUT_BYTES = 24_000


# ---------------------------------------------------------------------------
# AST helper: parent-pointer map
# ---------------------------------------------------------------------------

def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Return a child→parent mapping for every node in *tree*.

    Fixes the prior best's enclosing-function detection bug: iterating
    all nodes and matching by line range returns the *outermost* function
    when functions are nested, the opposite of what is wanted. The
    parent-pointer map correctly identifies the *innermost* enclosing scope
    by walking up the parent chain. Pattern proven in
    harness/tools/cross_reference.py's _parent_class() helper.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _innermost_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str | None:
    """Walk up parent pointers to find the nearest enclosing function name."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class DataFlowTool(Tool):
    name = "data_flow"
    description = (
        "Trace how a symbol (function, class, or attribute) is used across "
        "the workspace. Modes: 'reads' finds attribute reads "
        "(e.g. config.max_tool_turns); 'callers' finds direct call sites; "
        "'call_chain' returns callers-of-callers up to depth 2. "
        "Uses AST analysis — no external dependencies."
    )
    requires_path_check = False  # manual allowed_paths enforcement via _check_dir_root

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "Symbol to trace. For attribute reads use 'obj.attr' "
                        "notation; for callers use the bare function name."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["reads", "callers", "call_chain"],
                    "description": "Trace mode (default: callers).",
                    "default": "callers",
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search (default: config.workspace).",
                    "default": "",
                },
                "depth": {
                    "type": "integer",
                    "description": "call_chain depth: 1 or 2 (default: 1).",
                    "default": 1,
                },
            },
            "required": ["symbol"],
        }

    async def execute(self, config: HarnessConfig, **kwargs: Any) -> ToolResult:
        symbol: str = kwargs.get("symbol", "").strip()
        mode: str = kwargs.get("mode", "callers")
        root: str = kwargs.get("root", "")
        depth: int = min(int(kwargs.get("depth", 1)), 2)  # cap at 2

        if not symbol:
            return ToolResult(error="'symbol' is required", is_error=True)

        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        # Parse all files once
        parsed: dict[Path, ast.Module] = {}
        for f in py_files:
            try:
                parsed[f] = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                pass

        if mode == "reads":
            results = self._find_reads(symbol, parsed, search_root)
        elif mode == "callers":
            results = self._find_callers(symbol, parsed, search_root)
        elif mode == "call_chain":
            results = self._call_chain(symbol, depth, parsed, search_root)
        else:
            return ToolResult(
                error=f"Unknown mode {mode!r}; use reads/callers/call_chain",
                is_error=True,
            )

        output = self._safe_json(
            {"symbol": symbol, "mode": mode, "results": results},
            max_bytes=_MAX_OUTPUT_BYTES,
        )
        return ToolResult(output=output)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_reads(
        symbol: str,
        parsed: dict[Path, ast.Module],
        root: Path,
    ) -> list[dict]:
        """Find attribute reads matching obj.attr or bare attr."""
        parts = symbol.split(".", 1)
        obj_name = parts[0] if len(parts) == 2 else None
        attr_name = parts[1] if len(parts) == 2 else parts[0]

        hits: list[dict] = []
        for fpath, tree in parsed.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == attr_name:
                    if obj_name is None or (
                        isinstance(node.value, ast.Name)
                        and node.value.id == obj_name
                    ):
                        hits.append({
                            "file": str(fpath.relative_to(root)),
                            "line": node.lineno,
                            "context": ast.unparse(node),
                        })
        return hits

    @staticmethod
    def _find_callers(
        symbol: str,
        parsed: dict[Path, ast.Module],
        root: Path,
    ) -> list[dict]:
        """Find ast.Call nodes whose function name matches symbol."""
        hits: list[dict] = []
        for fpath, tree in parsed.items():
            parents = _build_parent_map(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name == symbol:
                    enclosing = _innermost_function(node, parents)
                    hits.append({
                        "file": str(fpath.relative_to(root)),
                        "line": node.lineno,
                        "enclosing_function": enclosing,
                    })
        return hits

    def _call_chain(
        self,
        symbol: str,
        depth: int,
        parsed: dict[Path, ast.Module],
        root: Path,
    ) -> dict:
        """Return callers at depth 1 and optionally depth 2.

        Depth is capped at 2 to avoid O(n³) traversals on large workspaces.
        At depth=2, for each unique enclosing_function from depth-1,
        we call _find_callers once — total = 1 + |unique_L1_functions| calls,
        each O(|parsed| × AST nodes).
        """
        l1 = self._find_callers(symbol, parsed, root)
        result: dict = {"l1_callers": l1}
        if depth >= 2:
            l2: dict[str, list[dict]] = {}
            seen: set[str] = set()
            for hit in l1:
                fn = hit.get("enclosing_function")
                if fn and fn not in seen:
                    seen.add(fn)
                    l2[fn] = self._find_callers(fn, parsed, root)
            result["l2_callers"] = l2
        return result
