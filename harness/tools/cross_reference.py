"""cross_reference — AST-based symbol cross-reference tool."""

from __future__ import annotations

import ast
import os
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
)
from harness.tools.base import Tool, ToolResult





_MAX_OUTPUT_BYTES = 8_192


class CrossReferenceTool(Tool):
    name = "cross_reference"
    description = (
        "Find where a Python symbol (function, method, or class) is defined and "
        "all its call sites across the codebase. Returns definition location, "
        "callers list, callees list, and test files. Uses AST parsing — no "
        "regex, no false positives from comments."
    )
    requires_path_check = True  # use base class security validation
    tags = frozenset({"analysis"})
    
    # Valid symbol pattern: standard Python identifier, optionally dot-qualified
    # e.g., "my_function", "ClassName.method_name"
    # REJECTS: consecutive dots, leading/trailing dots, directory traversal
    # Maximum qualification depth of 10 (0-9 repetitions of dot+identifier for total of 10)
    _VALID_SYMBOL_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*){0,9}$', re.ASCII)
    
    # Maximum depth for symbol qualification to prevent denial-of-service attacks
    # via deeply nested symbols like "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p"
    _MAX_SYMBOL_DEPTH = 10



    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "Symbol to look up. Supports 'func_name' and "
                        "'ClassName.method_name' forms. Maximum qualification depth "
                        f"is {self._MAX_SYMBOL_DEPTH} (e.g., 'a.b.c.d.e.f.g.h.i.j' is 10)."
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

        # Helper function to extract the base variable name from an expression
        def extract_base_name(node: ast.AST) -> str | None:
            """Extract the base variable name from an expression.
            
            For example:
            - `obj` -> "obj"
            - `obj.attr` -> "obj"
            - `obj.attr.method()` -> "obj"
            - `self.helper` -> "self"
            """
            if isinstance(node, ast.Name):
                return node.id
            elif isinstance(node, ast.Attribute):
                return extract_base_name(node.value)
            elif isinstance(node, ast.Call):
                # For method calls like obj.method(), check the base of the method
                if isinstance(node.func, ast.Attribute):
                    return extract_base_name(node.func.value)
            return None

        # Extract the base variable name from the function call
        base_name = extract_base_name(call_node.func.value)
        if not base_name:
            return False

        # Check if variable type matches target class
        if context and base_name in context and context[base_name] == class_name:
            return True
        # Special case: 'self' in instance methods
        if base_name == 'self' and context and context.get('self_class') == class_name:
            return True
        
        return False

    def _validate_symbol(self, symbol: str) -> None:
        """Validate symbol format and depth.
        
        Performs comprehensive validation:
        1. Checks depth against _MAX_SYMBOL_DEPTH
        2. Validates format against _VALID_SYMBOL_PATTERN
        3. Additional security checks
        
        Raises:
            ValueError: If symbol validation fails
        """
        # 1. Depth validation
        depth = symbol.count('.') + 1  # Count dots to get depth
        if depth > self._MAX_SYMBOL_DEPTH:
            raise ValueError(
                f"Symbol qualification depth {depth} exceeds maximum of {self._MAX_SYMBOL_DEPTH}"
            )
        
        # 2. Format validation
        if not re.match(self._VALID_SYMBOL_PATTERN, symbol):
            raise ValueError(f"Invalid symbol format: {symbol}")
        
        # 3. Additional security checks
        if '..' in symbol or symbol.startswith('.') or symbol.endswith('.'):
            raise ValueError(f"Potentially malicious symbol: '{symbol}'")

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
        
        # Validate symbol format, depth, and security
        symbol = symbol.strip()
        try:
            self._validate_symbol(symbol)
        except ValueError as e:
            return ToolResult(error=f"Symbol validation failed: {e}", is_error=True)

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
            source = read_file_atomically(fpath, allowed_paths=allowed)
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
            context = collector.context if class_name else {}

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
