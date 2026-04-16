"""feature_search — keyword-based feature discovery across the codebase.

Given a plain-English feature keyword (e.g. ``'checkpoint'``, ``'retry'``,
``'evaluation'``), finds all code that is related to that feature using four
complementary heuristics:

1. **Symbol names** — functions/classes/methods whose name contains the keyword.
2. **File names** — ``.py`` files whose basename contains the keyword.
3. **Comments & docstrings** — lines containing ``# …keyword…`` or string
   literals at the top of a function/class body.
4. **Config keys** — module-level assignments whose name contains the keyword
   (covers ``CHECKPOINT_DIR = …``, ``retry_limit: int = …``, etc.).

All analysis is pure AST + text scanning — no external dependencies.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

_MAX_OUTPUT_BYTES = 24_000


class FeatureSearchTool(Tool):
    """Find all code related to a feature keyword across the workspace."""

    name = "feature_search"
    description = (
        "Find all code related to a feature or concept keyword across the codebase. "
        "Searches: (1) function/class/method names containing the keyword, "
        "(2) Python files whose filename contains the keyword, "
        "(3) comments and docstrings mentioning the keyword, "
        "(4) module-level config/constant names containing the keyword. "
        "Returns structured results grouped by category. "
        "No external dependencies — pure AST and text analysis."
    )
    requires_path_check = False  # manual allowed_paths enforcement via _check_dir_root

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": (
                        "Feature keyword to search for "
                        "(e.g. 'checkpoint', 'retry', 'evaluation', 'auth'). "
                        "Case-insensitive; partial matches are included."
                    ),
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search (default: config.workspace).",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results per category (default: 30).",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 200,
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["symbols", "files", "comments", "config"]},
                    "description": (
                        "Which categories to include (default: all four). "
                        "Use a subset to narrow results: "
                        "['symbols'] for just function/class names, "
                        "['comments'] for just docstrings/inline comments."
                    ),
                    "default": ["symbols", "files", "comments", "config"],
                },
            },
            "required": ["keyword"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        keyword: str = "",
        root: str = "",
        max_results: int = 30,
        categories: list[str] | None = None,
    ) -> ToolResult:
        keyword = keyword.strip()
        if not keyword:
            return ToolResult(error="'keyword' is required and must be non-empty", is_error=True)

        search_root, allowed, err = self._check_dir_root(config, root)
        if err:
            return err

        # Clamp max_results
        max_results = max(1, min(200, max_results))

        # Default categories
        if categories is None:
            categories = ["symbols", "files", "comments", "config"]
        active = set(categories)

        kw_lower = keyword.lower()
        # Compile a case-insensitive regex for comment/docstring scanning
        kw_re = re.compile(re.escape(keyword), re.IGNORECASE)

        py_files = self._rglob_safe(search_root, "*.py", allowed)

        # --- Category: files ---
        file_hits: list[dict[str, Any]] = []
        if "files" in active:
            for fpath in py_files:
                if kw_lower in fpath.name.lower():
                    try:
                        rel = str(fpath.relative_to(search_root))
                    except ValueError:
                        rel = str(fpath)
                    file_hits.append({"file": rel})
                    if len(file_hits) >= max_results:
                        break

        # Parse files once for AST-based categories
        symbol_hits: list[dict[str, Any]] = []
        comment_hits: list[dict[str, Any]] = []
        config_hits: list[dict[str, Any]] = []

        needs_ast = active & {"symbols", "comments", "config"}

        for fpath in py_files:
            if not needs_ast:
                break
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            try:
                rel = str(fpath.relative_to(search_root))
            except ValueError:
                rel = str(fpath)

            # --- Category: comments (text scan — fast, no AST needed) ---
            if "comments" in active and len(comment_hits) < max_results:
                for lineno, line in enumerate(source.splitlines(), start=1):
                    stripped = line.strip()
                    # Match inline comments
                    if stripped.startswith("#") and kw_re.search(stripped):
                        comment_hits.append({
                            "file": rel,
                            "line": lineno,
                            "kind": "comment",
                            "text": stripped[:120],
                        })
                        if len(comment_hits) >= max_results:
                            break

            if not (active & {"symbols", "config"}):
                continue

            # Parse AST for symbols and config
            try:
                tree = ast.parse(source, filename=rel)
            except SyntaxError:
                continue

            lines = source.splitlines()

            # --- Category: symbols + comments (docstrings via AST) ---
            if "symbols" in active or "comments" in active:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        # Symbol name match
                        if "symbols" in active and len(symbol_hits) < max_results:
                            if kw_lower in node.name.lower():
                                kind = (
                                    "class" if isinstance(node, ast.ClassDef)
                                    else "async_function" if isinstance(node, ast.AsyncFunctionDef)
                                    else "function"
                                )
                                symbol_hits.append({
                                    "file": rel,
                                    "line": node.lineno,
                                    "kind": kind,
                                    "name": node.name,
                                })

                        # Docstring match
                        if "comments" in active and len(comment_hits) < max_results:
                            docstring = ast.get_docstring(node)
                            if docstring and kw_re.search(docstring):
                                # Use first matching line within the docstring
                                first_line = next(
                                    (ln for ln in docstring.splitlines() if kw_re.search(ln)),
                                    docstring.splitlines()[0],
                                )
                                comment_hits.append({
                                    "file": rel,
                                    "line": node.lineno,
                                    "kind": "docstring",
                                    "text": first_line.strip()[:120],
                                    "symbol": node.name,
                                })

            # --- Category: config (module-level assignments) ---
            if "config" in active and len(config_hits) < max_results:
                for node in ast.iter_child_nodes(tree):
                    if len(config_hits) >= max_results:
                        break
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and kw_lower in target.id.lower():
                                # Render value as short snippet
                                try:
                                    val_snippet = ast.unparse(node.value)[:60]
                                except Exception:
                                    val_snippet = "…"
                                config_hits.append({
                                    "file": rel,
                                    "line": node.lineno,
                                    "name": target.id,
                                    "value_snippet": val_snippet,
                                })
                                if len(config_hits) >= max_results:
                                    break
                    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                        if kw_lower in node.target.id.lower():
                            try:
                                val_snippet = ast.unparse(node.value)[:60] if node.value else "…"
                            except Exception:
                                val_snippet = "…"
                            config_hits.append({
                                "file": rel,
                                "line": node.lineno,
                                "name": node.target.id,
                                "value_snippet": val_snippet,
                            })

        results: dict[str, Any] = {
            "keyword": keyword,
            "files_scanned": len(py_files),
        }
        if "files" in active:
            results["files"] = file_hits
        if "symbols" in active:
            results["symbols"] = symbol_hits
        if "comments" in active:
            results["comments"] = comment_hits
        if "config" in active:
            results["config"] = config_hits

        return ToolResult(output=self._safe_json(results, max_bytes=_MAX_OUTPUT_BYTES))
