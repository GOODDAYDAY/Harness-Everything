"""project_map — generate a high-level project overview in one call.

Scans all Python files under a directory and produces:
- Module list with line counts, class counts, function counts
- Entry points (files with ``if __name__ == "__main__"``)
- Inter-module import graph (who imports whom)
- Summary stats (total files, total lines, total classes, total functions)

Much faster than tree + multiple reads for initial project orientation.
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ProjectMapTool(Tool):
    name = "project_map"
    description = (
        "Generate a high-level project map: modules with line/class/function counts, "
        "entry points, and inter-module import graph. One call replaces tree + many "
        "reads for project orientation. Scans Python files under a given directory."
    )
    requires_path_check = True
    tags = frozenset({"analysis"})

    MAX_FILES = 500
    _MAX_OUTPUT_CHARS = 30_000

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Root directory to scan. "
                        "Examples: '.' for entire project, 'harness/' for a package."
                    ),
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "Max directory depth to scan. "
                        "Use 2-3 for package overview, 5+ for deep scan."
                    ),
                },
                "include_tests": {
                    "type": "boolean",
                    "description": "Include test files (test_*.py, *_test.py). Default: false.",
                },
            },
            "required": ["path", "max_depth"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path: str,
        max_depth: int,
        include_tests: bool = False,
    ) -> ToolResult:
        if max_depth < 1:
            return ToolResult(error="max_depth must be >= 1", is_error=True)

        # Resolve root
        if os.path.isabs(path):
            root = Path(path)
        else:
            root = Path(config.workspace) / path

        if not root.is_dir():
            return ToolResult(error=f"Not a directory: {root}", is_error=True)

        # Collect Python files
        py_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Compute depth
            rel = Path(dirpath).relative_to(root)
            depth = len(rel.parts)
            if depth >= max_depth:
                dirnames.clear()
                continue

            # Skip hidden dirs and common non-source dirs
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in ("__pycache__", "node_modules", ".git", ".venv", "venv", "env")
            ]

            for f in filenames:
                if not f.endswith(".py"):
                    continue
                if not include_tests and (f.startswith("test_") or f.endswith("_test.py")):
                    continue
                py_files.append(Path(dirpath) / f)

                if len(py_files) >= self.MAX_FILES:
                    break
            if len(py_files) >= self.MAX_FILES:
                break

        if not py_files:
            return ToolResult(output=f"No Python files found under {root}")

        # Analyze each file
        modules: list[dict[str, Any]] = []
        entry_points: list[str] = []
        import_graph: dict[str, list[str]] = defaultdict(list)
        total_lines = 0
        total_classes = 0
        total_functions = 0
        errors: list[str] = []

        for fp in sorted(py_files):
            rel_path = str(fp.relative_to(root))
            info = self._analyze_file(fp, rel_path)
            if info is None:
                errors.append(rel_path)
                continue

            modules.append(info)
            total_lines += info["lines"]
            total_classes += info["classes"]
            total_functions += info["functions"]
            if info["is_entry"]:
                entry_points.append(rel_path)
            if info["imports"]:
                import_graph[rel_path] = info["imports"]

        # Build output
        sections: list[str] = []

        # Summary
        sections.append(
            f"Project: {root.name}/\n"
            f"  {len(modules)} modules, {total_lines} lines, "
            f"{total_classes} classes, {total_functions} functions"
        )
        if errors:
            sections[-1] += f", {len(errors)} parse errors"

        # Entry points
        if entry_points:
            sections.append(
                "Entry points:\n" + "\n".join(f"  {e}" for e in entry_points)
            )

        # Module table (sorted by line count descending)
        sorted_modules = sorted(modules, key=lambda m: m["lines"], reverse=True)
        table_lines = ["Modules (by size):"]
        table_lines.append(f"  {'lines':>6}  {'cls':>4}  {'fn':>4}  path")
        table_lines.append(f"  {'─' * 6}  {'─' * 4}  {'─' * 4}  {'─' * 30}")
        for m in sorted_modules:
            markers = ""
            if m["is_entry"]:
                markers += " [entry]"
            if m.get("has_main_class"):
                markers += " [main-class]"
            table_lines.append(
                f"  {m['lines']:>6}  {m['classes']:>4}  {m['functions']:>4}  {m['path']}{markers}"
            )
        sections.append("\n".join(table_lines))

        # Import graph (only internal imports, skip stdlib)
        if import_graph:
            graph_lines = ["Internal imports:"]
            for src, imports in sorted(import_graph.items()):
                if imports:
                    graph_lines.append(f"  {src}")
                    for imp in imports[:10]:
                        graph_lines.append(f"    -> {imp}")
                    if len(imports) > 10:
                        graph_lines.append(f"    ... +{len(imports) - 10} more")
            sections.append("\n".join(graph_lines))

        # Parse errors
        if errors:
            sections.append(
                "Parse errors:\n" + "\n".join(f"  {e}" for e in errors[:20])
            )

        output = "\n\n".join(sections)
        if len(output) > self._MAX_OUTPUT_CHARS:
            output = output[: self._MAX_OUTPUT_CHARS] + "\n... [truncated]"
        return ToolResult(output=output)

    def _analyze_file(self, filepath: Path, rel_path: str) -> dict[str, Any] | None:
        """Parse one file and extract metrics."""
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

        try:
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, ValueError):
            return None

        class_count = 0
        function_count = 0
        is_entry = False
        has_main_class = False
        imports: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_count += 1
                # Check if any class looks like a "main" class (has run/main method)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in ("run", "main", "execute"):
                            has_main_class = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_count += 1
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.append(node.module.split(".")[0])
                elif node.level > 0 and node.module:
                    # Relative import — record the module part
                    imports.append(f".{node.module}")
            elif isinstance(node, ast.If):
                # Check for if __name__ == "__main__"
                if (
                    isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"
                ):
                    is_entry = True

        # Deduplicate imports
        seen = set()
        unique_imports = []
        for imp in imports:
            if imp not in seen:
                seen.add(imp)
                unique_imports.append(imp)

        return {
            "path": rel_path,
            "lines": line_count,
            "classes": class_count,
            "functions": function_count,
            "is_entry": is_entry,
            "has_main_class": has_main_class,
            "imports": unique_imports,
        }
