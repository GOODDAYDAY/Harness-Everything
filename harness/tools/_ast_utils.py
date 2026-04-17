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





def dotted_name(node: ast.expr) -> str:
    """Flatten an attribute chain to a dotted string, e.g. ``os.path.join``.
    
    Returns the dotted name for Name or Attribute nodes, "<expr>" otherwise.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted_name(node.value)}.{node.attr}"
    return "<expr>"


def extract_calls(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return deduplicated list of call targets inside *func_node* (best-effort).
    
    Walks the function body and collects dotted names of all Call nodes.
    """
    seen: set[str] = set()
    calls: list[str] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if name not in seen:
                seen.add(name)
                calls.append(name)
    return calls


def safe_parse(source: str, filename: str = "<string>") -> ast.Module | None:
    """Parse source code, returning None on SyntaxError.
    
    A convenience wrapper for ast.parse that catches SyntaxError and returns
    None instead of raising. Useful when analyzing potentially malformed code.
    """
    try:
        return ast.parse(source, filename=filename)
    except (SyntaxError, MemoryError, RecursionError):
        return None


def parent_class(tree: ast.AST, target: ast.AST) -> str | None:
    """Return the *direct* parent ClassDef name of `target`, or None.
    
    Uses build_parent_map to walk upward from target until a ClassDef is found.
    """
    parent_map = build_parent_map(tree)
    node: ast.AST | None = target
    while node is not None:
        p = parent_map.get(id(node))
        if p is None:
            return None
        if isinstance(p, ast.ClassDef):
            return p.name
        node = p
    return None


def call_name(node: ast.Call) -> str | None:
    """Extract a string representation of a call node.
    
    Returns:
    - "func_name" for ast.Name nodes
    - "obj.method_name" for ast.Attribute nodes where value is a Name
    - "b.method_name" for nested attributes like a.b.method_name (keep last two)
    - "method_name" for other ast.Attribute nodes
    - None for other call types
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{attr}"
        # Nested attribute like a.b.c → keep b.c for readability
        if isinstance(node.func.value, ast.Attribute):
            return f"{node.func.value.attr}.{attr}"
        return attr
    return None


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> str:
    """Extract the signature line of a function from source lines.
    
    Args:
        node: The function AST node
        lines: List of source code lines (0-indexed)
    
    Returns:
        The signature line as a string, stripped of leading/trailing whitespace
    """
    return lines[node.lineno - 1].strip()


def extract_callees(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    cap: int = 30,
) -> list[str]:
    """Extract unique callee names from a function body.
    
    Walks the function AST and collects all call names, deduplicated.
    
    Args:
        node: Function AST node
        cap: Maximum number of callees to return
    
    Returns:
        List of unique callee names
    """
    seen: set[str] = set()
    result: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = call_name(n)
            if name and name not in seen:
                seen.add(name)
                result.append(name)
                if len(result) >= cap:
                    break
    return result


def innermost_function(node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    """Walk up parent pointers to find the nearest enclosing function name.
    
    Args:
        node: Starting AST node
        parents: Parent map from build_parent_map
    
    Returns:
        Name of the nearest enclosing function, or None if not inside a function
    """
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(id(current))
    return None
