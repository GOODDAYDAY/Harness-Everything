"""Private AST utilities shared across harness analysis tools.

NOT a Tool — not registered in __init__.py.  Import from this module to
avoid duplicating ast.parse / parent-map / function-walk boilerplate across
code_analysis, symbol_extractor, cross_reference, call_graph, data_flow,
and feature_search.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _read_file_atomically(path: Path | str, allowed_paths: list[Path] | None = None) -> str | None:
    """Read a file atomically to prevent TOCTOU symlink attacks.
    
    DEPRECATED: Use read_file_atomically from harness.core.security instead.
    
    Args:
        path: Path to the file to read.
        allowed_paths: Optional list of allowed directory paths for security containment.
            
    Returns:
        File content as string, or None if the file cannot be read securely.
    """
    import warnings
    warnings.warn(
        "_read_file_atomically is deprecated; import read_file_atomically from harness.core.security",
        DeprecationWarning,
        stacklevel=2
    )
    import os
    fd = None
    try:
        # 1. RESOLVE and VALIDATE path security FIRST
        abs_path = Path(path).resolve()
        
        # Check if the resolved path is within allowed paths (if provided)
        if allowed_paths is not None:
            if not any(abs_path.is_relative_to(allowed_path) 
                      for allowed_path in allowed_paths):
                return None
        
        # 2. Open file descriptor (with O_NOFOLLOW)
        open_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(abs_path), open_flags)
        
        # 3. FINAL VERIFICATION: Ensure opened fd matches the resolved path
        fd_stat = os.fstat(fd)
        try:
            path_stat = abs_path.stat()
        except OSError:
            return None
        
        if not (fd_stat.st_dev == path_stat.st_dev and fd_stat.st_ino == path_stat.st_ino):
            return None  # File was swapped after resolution but before open
        
        # 4. Read content
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as f:
            fd = None
            return f.read()
    except (OSError, PermissionError, UnicodeDecodeError):
        return None
    finally:
        # Only close the file descriptor if os.fdopen didn't take ownership
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_module(path: Path | str) -> tuple[ast.Module | None, str | None]:
    """Read *path* and return a parsed ``ast.Module`` with error message.

    Returns (ast.Module, None) on success, (None, error_message) on failure.
    Handles ``SyntaxError`` (invalid Python), ``OSError`` (file not readable),
    ``MemoryError``, and ``RecursionError`` so callers can iterate a file list
    and log diagnostic information instead of silently skipping.
    """
    try:
        source = _read_file_atomically(path)
        if source is None:
            return None, f"OSError reading {path}: cannot read file securely"
        return ast.parse(source, filename=str(path)), None
    except SyntaxError as exc:
        return None, f"SyntaxError in {path}: {exc}"
    except OSError as exc:
        return None, f"OSError reading {path}: {exc}"
    except MemoryError:
        return None, f"MemoryError parsing {path}: file too large or complex"
    except RecursionError:
        return None, f"RecursionError parsing {path}: AST too deeply nested"


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


def call_name(node: ast.Call, context: dict[str, str] | None = None) -> str | None:
    """Extract a string representation of a call node.
    
    Args:
        node: The call AST node
        context: Optional mapping from variable names to class names for
                 resolving instance method calls (e.g., {"self": "MyClass"})
    
    Returns:
    - "func_name" for ast.Name nodes
    - "ClassName.method_name" for ast.Attribute nodes where value is a Name
      and context maps the variable name to a class name
    - "obj.method_name" for ast.Attribute nodes where value is a Name
      (when no context mapping available)
    - "b.method_name" for nested attributes like a.b.method_name (keep last two)
    - "method_name" for other ast.Attribute nodes
    - None for other call types
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        # Try to resolve instance variable to class name using context
        if isinstance(node.func.value, ast.Name) and context:
            var_name = node.func.value.id
            class_name = context.get(var_name)
            if class_name:
                return f"{class_name}.{attr}"
            return f"{var_name}.{attr}"
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


def find_symbol_references(
    tree: ast.Module,
    symbol: str,
    filename: str
) -> dict[str, list[tuple[int, int]]]:
    """Find all references to a symbol in an AST tree.
    
    This consolidates AST traversal logic for finding definitions, callers,
    and callees of a given symbol, eliminating code duplication across tools.
    
    Args:
        tree: Parsed AST module
        symbol: Symbol name to search for (e.g., "my_func" or "MyClass.method")
        filename: Source filename for error reporting
    
    Returns:
        Dictionary with keys:
        - "definitions": List of (line, col) tuples for symbol definitions
        - "calls": List of (line, col) tuples for calls to the symbol
        - "references": List of (line, col) tuples for other references
    """
    result = {
        "definitions": [],
        "calls": [],
        "references": []
    }
    
    # Split symbol into parts for qualified name matching
    symbol_parts = symbol.split(".")
    is_qualified = len(symbol_parts) > 1
    
    # Build parent map for checking context
    parent_map = build_parent_map(tree)
    
    for node in ast.walk(tree):
        # Check for definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol_parts[-1]:
                if is_qualified:
                    # For qualified names like "ClassName.method", we need to check context
                    if len(symbol_parts) == 2:  # ClassName.method format
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            # Check if this method is inside the right class
                            parent = parent_map.get(id(node))
                            if isinstance(parent, ast.ClassDef) and parent.name == symbol_parts[0]:
                                result["definitions"].append((node.lineno, node.col_offset))
                        elif isinstance(node, ast.ClassDef):
                            # For class definitions, check if we're looking for just the class
                            result["definitions"].append((node.lineno, node.col_offset))
                else:
                    # Simple symbol name
                    result["definitions"].append((node.lineno, node.col_offset))
        
        # Check for calls
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                # Simple function call like func()
                if node.func.id == symbol_parts[-1] and not is_qualified:
                    result["calls"].append((node.lineno, node.col_offset))
            elif isinstance(node.func, ast.Attribute):
                # Method call like obj.method() or ClassName.method()
                if node.func.attr == symbol_parts[-1]:
                    if is_qualified:
                        # Check if the full dotted name matches
                        full_name = dotted_name(node.func)
                        if full_name == symbol:
                            result["calls"].append((node.lineno, node.col_offset))
                    else:
                        # Simple method name match
                        result["calls"].append((node.lineno, node.col_offset))
        
        # Check for other references (names)
        elif isinstance(node, ast.Name):
            if node.id == symbol_parts[-1] and not is_qualified:
                # Check context to avoid false positives
                # Skip if this is part of an attribute chain
                parent = parent_map.get(id(node))
                if not isinstance(parent, ast.Attribute):
                    result["references"].append((node.lineno, node.col_offset))
        
        # Check for attribute references (not calls)
        elif isinstance(node, ast.Attribute):
            if node.attr == symbol_parts[-1]:
                if is_qualified:
                    # Check if the full dotted name matches
                    full_name = dotted_name(node)
                    if full_name == symbol:
                        result["references"].append((node.lineno, node.col_offset))
                else:
                    # Simple attribute name match
                    result["references"].append((node.lineno, node.col_offset))
    
    return result


def collect_variable_context(tree: ast.AST) -> dict[str, str]:
    """Collect variable-to-class mapping context from an AST tree.
    
    Walks the AST and builds a context dictionary that maps variable names
    to class names for type inference. This is useful for resolving instance
    method calls like `obj.method()` when `obj` is known to be an instance
    of a specific class.
    
    Args:
        tree: AST tree to analyze
        
    Returns:
        Dictionary mapping variable names to class names
    """
    class VariableCollector(ast.NodeVisitor):
        def __init__(self):
            self.context = {}
            self.current_class = None

        def visit_ClassDef(self, node):
            old_class = self.current_class
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = old_class

        def visit_FunctionDef(self, node):
            # Map 'self' parameter to current class in instance methods
            if self.current_class and node.args.args:
                first_arg = node.args.args[0]
                if isinstance(first_arg, ast.arg) and first_arg.arg == 'self':
                    self.context['self'] = self.current_class
                    self.context['self_class'] = self.current_class
            self.generic_visit(node)

        def visit_Assign(self, node):
            # Simple type inference for: var = ClassName()
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and
                isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)):
                var_name = node.targets[0].id
                class_name = node.value.func.id
                self.context[var_name] = class_name
            self.generic_visit(node)

    collector = VariableCollector()
    collector.visit(tree)
    return collector.context