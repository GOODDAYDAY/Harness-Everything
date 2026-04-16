"""cross_reference — AST-based symbol cross-reference tool."""

from __future__ import annotations

import ast
import json
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools._ast_utils import build_parent_map
from harness.tools.base import Tool, ToolResult


def _parent_class(tree: ast.AST, target: ast.AST) -> str | None:
    """Return the *direct* parent ClassDef name of `target`, or None."""
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


def _sig(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> str:
    return lines[node.lineno - 1].strip()


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
        return node.func.attr
    return None


def _extract_callees(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    cap: int = 30,
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if name and name not in seen:
                seen.add(name)
                result.append(name)
                if len(result) >= cap:
                    break
    return result


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

    async def execute(
        self,
        config: HarnessConfig,
        symbol: str,
        root: str = "",
        include_tests: bool = True,
    ) -> ToolResult:
        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        parts = symbol.strip().split(".", 1)
        class_name = parts[0] if len(parts) == 2 else None
        func_name = parts[1] if len(parts) == 2 else parts[0]

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        definition: dict[str, Any] | None = None
        callers: list[dict[str, Any]] = []
        callees: list[str] = []
        test_files: list[str] = []

        for fpath in py_files:
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(fpath))
            except SyntaxError:
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
                        or _parent_class(tree, node) == class_name
                    )
                    if name_match and class_match and definition is None:
                        definition = {
                            "file": rel,
                            "line": node.lineno,
                            "signature": _sig(node, lines),
                        }
                        callees = _extract_callees(node)
                elif class_name is None and isinstance(node, ast.ClassDef):
                    if node.name == func_name and definition is None:
                        definition = {
                            "file": rel,
                            "line": node.lineno,
                            "signature": lines[node.lineno - 1].strip(),
                        }

                # Caller detection
                if isinstance(node, ast.Call) and len(callers) < 50:
                    cname = _call_name(node)
                    if cname and func_name in cname:
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
                if ("test" in rel or "spec" in rel) and func_name in source:
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

        # Compact JSON; trim callers list further if output exceeds budget
        output = json.dumps(result)
        if len(output) > _MAX_OUTPUT_BYTES:
            # Trim callers to fit within budget
            while len(output) > _MAX_OUTPUT_BYTES and result["callers"]:
                result["callers"] = result["callers"][: len(result["callers"]) - 5]
                result["truncated"] = True
                output = json.dumps(result)

        return ToolResult(output=output)
