"""code_analysis — AST-based static analysis of Python source files.

Provides symbol tables, import maps, call graphs, and cyclomatic-complexity
estimates without executing the code.  Pure stdlib — no extra dependencies.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

# ---------------------------------------------------------------------------
# Branch-count heuristic for cyclomatic complexity.
# Each node type below adds 1 to the branch count.
# ---------------------------------------------------------------------------
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.Assert,
    ast.comprehension,
)


def _complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return branch count (1-based cyclomatic complexity proxy) for *func_node*."""
    count = 1  # base path
    for node in ast.walk(func_node):
        if isinstance(node, _BRANCH_NODES):
            count += 1
        elif isinstance(node, ast.BoolOp):
            # each extra `and`/`or` operand is an extra path
            count += len(node.values) - 1
    return count


def _dotted_name(node: ast.expr) -> str:
    """Flatten an attribute chain to a dotted string, e.g. ``os.path.join``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return "<expr>"


def _calls_in(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return deduplicated list of call targets inside *func_node* (best-effort)."""
    seen: set[str] = set()
    calls: list[str] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name not in seen:
                seen.add(name)
                calls.append(name)
    return calls


def _analyse_source(source: str, filename: str) -> dict[str, Any]:
    """Parse *source* and return a structured analysis dict."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return {"error": f"SyntaxError at line {exc.lineno}: {exc.msg}"}

    imports: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        # ---- imports ----
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "import",
                    "module": alias.name,
                    "alias": alias.asname,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append({
                    "type": "from",
                    "module": module,
                    "name": alias.name,
                    "alias": alias.asname,
                    "line": node.lineno,
                })

    # Walk only top-level statements for classes/functions so we don't
    # double-count nested defs in the symbols table, but we *do* want nested
    # functions in the per-function detail block, so a second walk handles that.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_async = isinstance(node, ast.AsyncFunctionDef)
            args = [a.arg for a in node.args.args]
            complexity = _complexity(node)
            calls = _calls_in(node)
            symbols.append({
                "kind": "async_function" if is_async else "function",
                "name": node.name,
                "line": node.lineno,
                "args": args,
                "complexity": complexity,
            })
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "is_async": is_async,
                "args": args,
                "complexity": complexity,
                "calls": calls,
            })
        elif isinstance(node, ast.ClassDef):
            # Collect methods inside the class
            methods: list[dict[str, Any]] = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(child, ast.AsyncFunctionDef)
                    args = [a.arg for a in child.args.args]
                    complexity = _complexity(child)
                    calls = _calls_in(child)
                    methods.append({
                        "name": child.name,
                        "line": child.lineno,
                        "is_async": is_async,
                        "args": args,
                        "complexity": complexity,
                        "calls": calls,
                    })
                    functions.append({
                        "name": f"{node.name}.{child.name}",
                        "line": child.lineno,
                        "is_async": is_async,
                        "args": args,
                        "complexity": complexity,
                        "calls": calls,
                    })
            base_names = [_dotted_name(b) for b in node.bases]
            symbols.append({
                "kind": "class",
                "name": node.name,
                "line": node.lineno,
                "bases": base_names,
                "methods": [m["name"] for m in methods],
                "method_count": len(methods),
            })
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Module-level constants / annotated assignments
            targets: list[str] = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        targets.append(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets.append(node.target.id)
            for name in targets:
                if name.isupper():  # treat ALL_CAPS as constants
                    symbols.append({
                        "kind": "constant",
                        "name": name,
                        "line": node.lineno,
                    })

    # Summary stats
    total_lines = source.count("\n") + 1
    avg_complexity = (
        round(sum(f["complexity"] for f in functions) / len(functions), 1)
        if functions else 0.0
    )
    high_complexity = [f for f in functions if f["complexity"] >= 10]

    return {
        "total_lines": total_lines,
        "imports": imports,
        "symbols": symbols,
        "functions": functions,
        "summary": {
            # top-level classes
            "classes": sum(1 for s in symbols if s["kind"] == "class"),
            # ALL callables: top-level functions + class methods (full functions list)
            "functions": len(functions),
            "imports": len(imports),
            "avg_complexity": avg_complexity,
            "high_complexity_functions": [f["name"] for f in high_complexity],
        },
    }


def _format_analysis(filename: str, analysis: dict[str, Any]) -> str:
    """Render an analysis dict as a human-readable text block."""
    if "error" in analysis:
        return f"=== {filename} ===\nERROR: {analysis['error']}\n"

    s = analysis["summary"]
    lines: list[str] = [
        f"=== {filename} ===",
        f"Lines: {analysis['total_lines']}  "
        f"Classes: {s['classes']}  "
        f"Functions: {s['functions']}  "
        f"Imports: {s['imports']}  "
        f"Avg complexity: {s['avg_complexity']}",
    ]

    if s["high_complexity_functions"]:
        lines.append(
            f"High-complexity (≥10): {', '.join(s['high_complexity_functions'])}"
        )

    # Imports
    if analysis["imports"]:
        lines.append("\nImports:")
        for imp in analysis["imports"]:
            if imp["type"] == "import":
                alias_str = f" as {imp['alias']}" if imp["alias"] else ""
                lines.append(f"  L{imp['line']:>4}  import {imp['module']}{alias_str}")
            else:
                alias_str = f" as {imp['alias']}" if imp["alias"] else ""
                lines.append(
                    f"  L{imp['line']:>4}  from {imp['module']} import {imp['name']}{alias_str}"
                )

    # Symbols (classes + top-level functions)
    if analysis["symbols"]:
        lines.append("\nSymbols:")
        for sym in analysis["symbols"]:
            kind = sym["kind"]
            if kind == "class":
                bases_str = f"({', '.join(sym['bases'])})" if sym["bases"] else ""
                lines.append(
                    f"  L{sym['line']:>4}  class {sym['name']}{bases_str}"
                    f"  [{sym['method_count']} method(s): {', '.join(sym['methods'])}]"
                )
            elif kind in ("function", "async_function"):
                prefix = "async def" if kind == "async_function" else "def"
                args_str = ", ".join(sym["args"])
                lines.append(
                    f"  L{sym['line']:>4}  {prefix} {sym['name']}({args_str})"
                    f"  complexity={sym['complexity']}"
                )
            elif kind == "constant":
                lines.append(f"  L{sym['line']:>4}  {sym['name']}  (constant)")

    # Call graph (only functions with non-trivial call lists)
    call_entries = [
        f for f in analysis["functions"]
        if f["calls"]
    ]
    if call_entries:
        lines.append("\nCall graph (outgoing calls per function):")
        for func in call_entries:
            prefix = "async " if func["is_async"] else ""
            calls_str = ", ".join(func["calls"][:10])  # cap display at 10
            suffix = f" (+{len(func['calls']) - 10} more)" if len(func["calls"]) > 10 else ""
            lines.append(f"  {prefix}{func['name']} → {calls_str}{suffix}")

    return "\n".join(lines)


class CodeAnalysisTool(Tool):
    """AST-based static analyser for Python source files.

    Analyses one file or all ``.py`` files under a directory and reports:

    * Symbol table — classes, functions (with args), module-level constants
    * Import map — every ``import`` / ``from … import`` with line numbers
    * Call graph — which functions each function calls (best-effort)
    * Complexity — branch-count proxy for cyclomatic complexity per function,
      plus a list of functions with complexity ≥ 10
    * Summary stats — line count, class/function counts, average complexity

    Output is plain text (``format="text"``) or JSON (``format="json"``).
    No code is executed — analysis is 100% static via ``ast.parse``.
    """

    name = "code_analysis"
    description = (
        "AST-based static analysis of Python source files. "
        "Given a file or directory path, reports: symbol table (classes, functions, "
        "constants with line numbers), import map, outgoing call graph per function, "
        "and cyclomatic-complexity proxy. "
        "Supports a file glob filter (default: **/*.py) and output in text or JSON. "
        "No code is executed — pure static analysis."
    )
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to analyse. "
                        "If a directory, all matching Python files are analysed."
                    ),
                },
                "file_glob": {
                    "type": "string",
                    "description": "Glob pattern when path is a directory (default: **/*.py)",
                    "default": "**/*.py",
                },
                "format": {
                    "type": "string",
                    "description": "Output format: 'text' (default) or 'json'",
                    "enum": ["text", "json"],
                    "default": "text",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of files to analyse (default: 50)",
                    "default": 50,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path: str,
        file_glob: str = "**/*.py",
        format: str = "text",  # noqa: A002
        limit: int = 50,
    ) -> ToolResult:
        resolved, err = self._resolve_and_check(config, path)
        if err:
            return err

        p = Path(resolved)

        # Collect target files
        if p.is_file():
            if not p.suffix == ".py":
                return ToolResult(
                    error=f"Not a Python file: {resolved}", is_error=True
                )
            files = [p]
        elif p.is_dir():
            matched = sorted(
                p.glob(file_glob),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            files = [f for f in matched if f.is_file() and f.suffix == ".py"][:limit]
            if not files:
                return ToolResult(
                    output=f"No Python files matched '{file_glob}' in {resolved}"
                )
        else:
            return ToolResult(error=f"Path not found: {resolved}", is_error=True)

        # Analyse each file
        results: dict[str, Any] = {}
        for fpath in files:
            rel = str(fpath.relative_to(p.parent) if p.is_file() else fpath.relative_to(p))
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                results[rel] = {"error": str(exc)}
                continue
            results[rel] = _analyse_source(source, rel)

        # Render output
        if format == "json":
            try:
                text = json.dumps(results, indent=2)
            except Exception as exc:
                return ToolResult(error=f"JSON serialisation failed: {exc}", is_error=True)
            return ToolResult(output=text)

        # Default: text
        parts: list[str] = []
        for filename, analysis in results.items():
            parts.append(_format_analysis(filename, analysis))

        # Aggregate cross-file summary when multiple files were analysed
        if len(results) > 1:
            total_lines = sum(
                a.get("total_lines", 0) for a in results.values() if "error" not in a
            )
            total_classes = sum(
                a.get("summary", {}).get("classes", 0)
                for a in results.values()
                if "error" not in a
            )
            total_fns = sum(
                a.get("summary", {}).get("functions", 0)
                for a in results.values()
                if "error" not in a
            )
            all_complexities = [
                f["complexity"]
                for a in results.values()
                if "error" not in a
                for f in a.get("functions", [])
            ]
            overall_avg = (
                round(sum(all_complexities) / len(all_complexities), 1)
                if all_complexities else 0.0
            )
            high = [
                f"{fname}::{func['name']}"
                for fname, a in results.items()
                if "error" not in a
                for func in a.get("functions", [])
                if func["complexity"] >= 10
            ]
            parts.append(
                "\n=== AGGREGATE SUMMARY ===\n"
                f"Files analysed : {len(results)}\n"
                f"Total lines    : {total_lines}\n"
                f"Total classes  : {total_classes}\n"
                f"Total functions: {total_fns}\n"
                f"Avg complexity : {overall_avg}\n"
                + (f"High-complexity: {', '.join(high)}\n" if high else "")
            )

        return ToolResult(output="\n\n".join(parts))
