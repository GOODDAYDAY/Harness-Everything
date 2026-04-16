"""Private AST utilities shared across harness analysis tools.

NOT a Tool — not registered in __init__.py.  Import from this module to
avoid duplicating ast.parse / parent-map / function-walk boilerplate across
code_analysis, symbol_extractor, cross_reference, call_graph, data_flow,
and feature_search.
"""

from __future__ import annotations

import ast
from pathlib import Path


def parse_module(path: Path | str) -> ast.Module | None:
    """Read *path* and return a parsed ``ast.Module``, or ``None`` on failure.

    Handles both ``SyntaxError`` (invalid Python) and ``OSError`` (file not
    readable) so callers can iterate a file list and simply skip ``None``
    returns instead of repeating try/except in each tool.
    """
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
        return ast.parse(source, filename=str(path))
    except (SyntaxError, OSError):
        return None


def build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Return an ``id(child) → parent`` mapping for every node in *tree*.

    Built in a single O(n) walk.  Use ``parent_map[id(node)]`` to navigate
    upward.  Callers that previously called ``ast.walk`` twice — once to
    build the map and once to use it — can share one call to this function.
    """
    parent_map: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[id(child)] = node
    return parent_map


def walk_functions(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every ``FunctionDef`` / ``AsyncFunctionDef`` node in *tree*.

    A thin wrapper around ``ast.walk`` so tools can write::

        for fn in walk_functions(tree):
            ...

    instead of repeating the ``isinstance`` guard everywhere.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
