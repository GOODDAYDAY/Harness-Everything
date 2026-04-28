"""cross_reference — AST-based symbol cross-reference tool."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.core.security import read_file_atomically
from harness.tools._ast_utils import (
    parent_class,
    function_signature,
    call_name,
    extract_callees,
    safe_parse,
    collect_variable_context,
)
from harness.tools.base import Tool, ToolResult





_MAX_OUTPUT_BYTES = 8_192

# Core identifier pattern for Python symbols (ASCII only for security)
_SYMBOL_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$', re.ASCII)


class CrossReferenceTool(Tool):
    name = "cross_reference"
    description = (
        "Find where a Python symbol (function, method, or class) is defined and "
        "all its call sites across the codebase. Returns definition location, "
        "callers list, callees list, and test files. Uses AST parsing — no "
        "regex, no false positives from comments. "
        "Use this when you want the full picture for a single symbol: where it "
        "is defined, every caller, every callee, and which tests exercise it — "
        "all in one call. "
        "Prefer data_flow when you only need one of reads/callers/call_chain and "
        "want control over traversal depth. "
        "Prefer call_graph when you want to trace outgoing calls recursively "
        "(forward/downward) from a root function. "
        "Prefer grep_search for non-Python files or string-literal occurrences."
    )
    requires_path_check = True  # use base class security validation
    tags = frozenset({"analysis"})
    
    # Valid symbol pattern: standard Python identifier, optionally dot-qualified
    # e.g., "my_function", "ClassName.method_name"
    # REJECTS: consecutive dots, leading/trailing dots, directory traversal
    # Allows 1-10 identifiers (0-9 dots), matching _MAX_SYMBOL_IDENTIFIERS=10
    # Note: We need to extract just the core pattern without ^ and $ anchors
    _CORE_IDENTIFIER_PATTERN = r'[a-zA-Z_][a-zA-Z0-9_]*'
    _VALID_SYMBOL_PATTERN = re.compile(r'^' + _CORE_IDENTIFIER_PATTERN + r'(?:\.' + _CORE_IDENTIFIER_PATTERN + r'){0,9}$', re.ASCII)
    
    # Maximum total identifiers (e.g., "a.b.c" has 3 identifiers) to prevent
    # denial-of-service attacks via deeply nested symbols like "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p"
    # Matches regex pattern {0,9} (1-10 identifiers total).
    _MAX_SYMBOL_IDENTIFIERS = 10

    def _read_file_atomically(
        self,
        path: Path,
        allowed_paths: list[Path],
    ) -> str | None:
        """Thin wrapper around security.read_file_atomically for testability.

        Returns the file contents as a string, or None if the path is outside
        the allowed directories or if any security check fails.
        """
        return read_file_atomically(path, allowed_paths)

    def _validate_symbol_format(self, symbol: str) -> tuple[bool, str]:
        """
        Validate symbol format against security constraints.
        Returns a tuple (is_valid, error_message).
        If is_valid is True, error_message is empty string.
        """
        # Combined check for empty or whitespace-only symbols
        if not symbol or not symbol.strip():
            return False, f"Symbol cannot be empty or whitespace-only: {repr(symbol)}"

        # Explicit depth check first for clearer error messages
        identifiers = symbol.split('.')
        if len(identifiers) > self._MAX_SYMBOL_IDENTIFIERS:
            # Enhanced error message for security auditing
            return False, f"Symbol '{symbol}' exceeds maximum depth of {self._MAX_SYMBOL_IDENTIFIERS} identifiers (found {len(identifiers)})."

        # Defense-in-depth: regex validation
        if self._VALID_SYMBOL_PATTERN.fullmatch(symbol) is None:
            return False, f"Invalid symbol format: {symbol}. Must be ASCII, start with a letter/underscore, and contain at most 10 identifiers."

        return True, ""

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "Symbol to look up. Supports 'func_name' and "
                        "'ClassName.method_name' forms. "
                        "Examples: 'my_function', 'MyClass', 'MyClass.method_name'. "
                        "Maximum qualification depth "
                        f"is {self._MAX_SYMBOL_IDENTIFIERS} (e.g., 'a.b.c.d.e.f.g.h.i.j' is 10)."
                    ),
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search (default: config.workspace).",
                    "default": "",
                },
                "include_tests": {
                    "type": "boolean",
                    "description": "Include test files in results (default: true).",
                    "default": True,
                },
            },
            "required": ["symbol"],
        }

    def _is_instance_method_call(
        self,
        call_node: ast.Call,
        class_name: str,
        func_name: str,
        context: dict[str, str] | None
    ) -> bool:
        """Check if a Call node represents an instance method call of target class."""
        if not isinstance(call_node.func, ast.Attribute):
            return False
        if call_node.func.attr != func_name:
            return False

        # Helper: extract base variable name AND chain depth
        def extract_base_and_depth(node: ast.AST, depth: int = 0):
            """Return (base_var_name, chain_depth) for an expression.

            chain_depth counts the number of attribute accesses / intermediate
            calls between the base variable and the final method call.  A
            depth > 0 means we can no longer reliably infer the type from
            the context (e.g. ``self.helper.process().method``).
            """
            if isinstance(node, ast.Name):
                return node.id, depth
            elif isinstance(node, ast.Attribute):
                return extract_base_and_depth(node.value, depth + 1)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    return extract_base_and_depth(node.func.value, depth + 1)
            return None, depth

        base_name, chain_depth = extract_base_and_depth(call_node.func.value)
        if not base_name:
            return False

        # Direct call on a typed variable: ``obj.method()`` where context maps
        # ``obj`` → a known class.  Only trust this for depth-0 calls (no
        # intermediate attribute traversal).
        if chain_depth == 0 and context and base_name in context:
            return context[base_name] == class_name

        # Direct ``self.method()`` inside the class's own methods.
        if chain_depth == 0 and base_name == 'self':
            if context and context.get('self_class') == class_name:
                return True
            # self is from a different class — not a match at depth-0.
            if context and 'self_class' in context:
                return False

        # For chained calls (depth > 0) or variables whose type is unknown, use
        # optimistic matching: return True to maximise recall.  False positives
        # are acceptable; the caller can inspect results manually.
        return True

    def validate_symbol(self, symbol: str) -> str:
        """Public interface for symbol validation. Returns the symbol if valid, otherwise raises ValueError.
        
        Args:
            symbol: The symbol string to validate
            
        Returns:
            The validated and stripped symbol string
            
        Raises:
            ValueError: If symbol validation fails with detailed error message
        """
        # Make a copy to strip and validate
        symbol_to_validate = symbol.strip()
        is_valid, error_msg = self._validate_symbol_format(symbol_to_validate)
        if not is_valid:
            raise ValueError(f"Symbol validation failed: {error_msg}")
        return symbol_to_validate

    async def execute(
        self,
        config: HarnessConfig,
        symbol: str,
        root: str = "",
        include_tests: bool = True,
    ) -> ToolResult:
        # Use _check_dir_root to validate the path and get allowed paths
        search_root, allowed, err_result = self._check_dir_root(config, root)
        if err_result:
            return err_result
        
        # Validate symbol format, depth, and security using consolidated validation
        try:
            validated_symbol = self.validate_symbol(symbol)
        except ValueError as e:
            return ToolResult(error=f"Symbol validation failed: {str(e)}", is_error=True)
        logging.getLogger(__name__).debug(f"Validated symbol: {validated_symbol}")
        
        symbol = validated_symbol

        parts = symbol.strip().split(".", 1)
        class_name = parts[0] if len(parts) == 2 else None
        func_name = parts[1] if len(parts) == 2 else parts[0]

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        definition: dict[str, Any] | None = None
        callers: list[dict[str, Any]] = []
        callees: list[str] = []
        test_files: list[str] = []
        
        # Compile regex pattern once for efficiency
        # Use negative lookbehind/lookahead instead of \b to handle underscores correctly
        test_pattern = re.compile(rf'(?<!\w){re.escape(func_name)}(?!\w)') if include_tests else None

        for fpath in py_files:
            # Use atomic file reading to prevent TOCTOU symlink attacks
            source = self._read_file_atomically(fpath, allowed_paths=allowed)
            if source is None:
                continue
            
            try:
                tree = safe_parse(source, filename=str(fpath))
                if tree is None:
                    continue
            except Exception:
                continue

            try:
                rel = str(fpath.relative_to(search_root))
            except ValueError:
                rel = str(fpath)
            lines = source.splitlines()

            # Collect context for variable types if looking for a class method
            context = collect_variable_context(tree) if class_name else {}

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name_match = node.name == func_name
                    class_match = (
                        class_name is None
                        or parent_class(tree, node) == class_name
                    )
                    if name_match and class_match and definition is None:
                        definition = {
                            "file": rel,
                            "line": node.lineno,
                            "signature": function_signature(node, lines),
                        }
                        # Append new callees, respecting the global limit
                        new_callees = extract_callees(node)
                        for callee in new_callees:
                            if len(callees) >= 30:
                                break
                            callees.append(callee)
                elif class_name is None and isinstance(node, ast.ClassDef):
                    if node.name == func_name and definition is None:
                        definition = {
                            "file": rel,
                            "line": node.lineno,
                            "signature": lines[node.lineno - 1].strip(),
                        }

                # Caller detection
                if isinstance(node, ast.Call) and len(callers) < 50:
                    cname = call_name(node, context)
                    match = False

                    if class_name:
                        expected = f"{class_name}.{func_name}"
                        # 1. Direct class method call
                        if cname == expected:
                            match = True
                        # 2. Instance method call via the new helper
                        elif self._is_instance_method_call(node, class_name, func_name, context):
                            match = True
                    else:
                        match = cname == func_name
                    
                    if match:
                        lineno = getattr(node, "lineno", 0)
                        snippet = (
                            lines[lineno - 1].strip()
                            if lines and lineno > 0
                            else ""
                        )
                        callers.append(
                            {"file": rel, "line": lineno, "snippet": snippet}
                        )

            # Test file detection
            if include_tests and len(test_files) < 20:
                # More precise test file detection
                filename = Path(rel).name
                is_test_file = (
                    filename.startswith("test_") or
                    filename.endswith("_test.py") or
                    filename.endswith("_spec.py")
                )
                # Use pre-compiled pattern for efficiency
                if is_test_file and test_pattern and test_pattern.search(source):
                    test_files.append(rel)

        # Proactive snippet truncation to prevent _safe_json truncation from corrupting output
        MAX_SNIPPET_LENGTH = 200
        for caller in callers:
            snippet = caller.get("snippet", "")
            if len(snippet) > MAX_SNIPPET_LENGTH:
                caller["snippet"] = snippet[:MAX_SNIPPET_LENGTH] + "..."

        result: dict[str, Any] = {
            "symbol": symbol,
            "definition": definition,
            "callers": callers,
            "callees": callees,
            "test_files": test_files,
            "files_scanned": len(py_files),
            "truncated": len(callers) >= 50 or len(callees) >= 30,
        }

        # Use the base class's safe JSON serialization
        output = self._safe_json(result, max_bytes=_MAX_OUTPUT_BYTES)
        return ToolResult(output=output)


# Verify the refactored regex pattern works correctly
assert CrossReferenceTool._VALID_SYMBOL_PATTERN.fullmatch("my.valid.symbol") is not None
