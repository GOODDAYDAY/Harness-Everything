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
    requires_path_check = False  # manual allowed_paths enforcement in execute()
    tags = frozenset({"analysis"})

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

    def _get_allowed_paths(self, config: HarnessConfig, resolved_root: str) -> list[Path]:
        """Get allowed paths for _rglob_safe.
        
        Returns the list of allowed Path objects that the search root must be within.
        This mimics the logic from Tool._check_dir_root but only returns the allowed list.
        """
        import os
        # Use os.path.realpath for consistency with HarnessConfig.is_path_allowed()
        allowed = [Path(os.path.realpath(p)) for p in config.allowed_paths]
        return allowed

    async def execute(
        self,
        config: HarnessConfig,
        symbol: str,
        root: str = "",
        include_tests: bool = True,
    ) -> ToolResult:
        # Always use _resolve_and_check to validate the path, even for default workspace
        resolved_root, err_result = self._resolve_and_check(config, root)
        if err_result:
            return err_result
        search_root = Path(resolved_root)
        
        # Get allowed paths for _rglob_safe from config
        allowed = self._get_allowed_paths(config, resolved_root)

        parts = symbol.strip().split(".", 1)
        class_name = parts[0] if len(parts) == 2 else None
        func_name = parts[1] if len(parts) == 2 else parts[0]

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        definition: dict[str, Any] | None = None
        callers: list[dict[str, Any]] = []
        callees: list[str] = []
        test_files: list[str] = []
        
        # Compile regex pattern once for efficiency
        test_pattern = re.compile(rf'\b{re.escape(func_name)}\b') if include_tests else None

        for fpath in py_files:
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
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
                            match = cname == expected
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
