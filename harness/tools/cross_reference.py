"""cross_reference — AST-based symbol cross-reference tool."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
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
    _VALID_SYMBOL_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$')

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": (
                        "Symbol to look up. Supports 'func_name' and "
                        "'ClassName.method_name' forms."
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
        
        # Validate symbol format to prevent injection of malicious characters
        if not self._VALID_SYMBOL_PATTERN.fullmatch(symbol.strip()):
            return ToolResult(error=f"Invalid symbol format: '{symbol}'", is_error=True)

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
            # Security containment check first to avoid TOCTOU symlink attacks
            try:
                abs_path = fpath.resolve()
                if not any(abs_path == allowed_path or abs_path.is_relative_to(allowed_path) for allowed_path in allowed):
                    continue  # Skip files outside allowed paths
            except Exception:
                continue
            
            # Read file content after security check
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
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
                    cname = call_name(node)
                    if cname:
                        # For class methods: check if cname matches "ClassName.method_name"
                        # For standalone functions: check if cname matches "func_name"
                        if class_name:
                            # Looking for ClassName.method_name
                            expected = f"{class_name}.{func_name}"
                            # Match any of:
                            # 1. Direct class method call: MyClass.method_name
                            # 2. Instance method call: *.method_name (where * is any attribute chain)
                            # 3. Bare method name: method_name (when call_name returns just the method name)
                            match = (cname == expected or 
                                    cname.endswith(f".{func_name}") or 
                                    cname == func_name)
                        else:
                            # Looking for standalone function
                            # Use exact match to avoid false positives like "test" matching "test_function"
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
