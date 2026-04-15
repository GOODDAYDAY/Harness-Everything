"""semantic_search — token-overlap semantic identifier search tool."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class SemanticSearchTool(Tool):
    name = "semantic_search"
    description = (
        "Find Python identifiers (functions, classes, methods) whose names are "
        "semantically related to a plain-English concept using token-overlap "
        "scoring. No external ML dependency. Useful for finding code relevant "
        "to a concept when you do not know the exact symbol name."
    )
    requires_path_check = False  # manual allowed_paths enforcement in execute()

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "concept": {
                    "type": "string",
                    "description": (
                        "Plain-English concept to search for "
                        "(e.g. 'file permission check', 'retry logic')."
                    ),
                },
                "root": {
                    "type": "string",
                    "description": "Directory to search (default: config.workspace).",
                    "default": "",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 20).",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["concept"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        concept: str,
        root: str = "",
        top_k: int = 20,
    ) -> ToolResult:
        # Path security — enforce against allowed_paths
        search_root = Path(root).resolve() if root else Path(config.workspace).resolve()
        allowed = [Path(p).resolve() for p in config.allowed_paths]
        if not any(
            search_root == a or search_root.is_relative_to(a) for a in allowed
        ):
            return ToolResult(
                error=(
                    f"PERMISSION ERROR: root {str(search_root)!r} is outside "
                    f"allowed_paths {config.allowed_paths}"
                ),
                is_error=True,
            )

        # Clamp top_k to valid range
        top_k = max(1, min(100, top_k))

        # Split concept into lowercase tokens for scoring
        concept_tokens = [t.lower() for t in concept.split() if t]

        py_files = sorted(search_root.rglob("*.py"))[:500]

        # Collect (score, identifier, file, line) tuples
        hits: list[tuple[int, str, str, int]] = []

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

            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    ident = node.name.lower()
                    score = sum(
                        1 for token in concept_tokens if token in ident
                    )
                    if score > 0:
                        hits.append((score, node.name, rel, node.lineno))

        # Sort by score descending, then by identifier name for stability
        hits.sort(key=lambda h: (-h[0], h[1]))

        results = [
            {
                "identifier": name,
                "file": rel,
                "line": lineno,
                "score": score,
            }
            for score, name, rel, lineno in hits[:top_k]
        ]

        output = json.dumps(
            {
                "concept": concept,
                "results": results,
                "files_scanned": len(py_files),
                "total_hits": len(hits),
            }
        )
        return ToolResult(output=output)
