"""ast_rename — AST-based Python symbol rename across the codebase.

Safer than regex find_replace: only renames actual Python identifiers,
not occurrences in strings, comments, or unrelated scopes.

Supports renaming:
- Top-level functions
- Top-level classes
- Module-level variables / constants
- Method names within a specific class
- Import references (``from mod import old_name`` → ``from mod import new_name``)

Does NOT rename:
- Local variables inside functions (too ambiguous without type inference)
- Dynamic attribute access (``getattr(obj, "name")``)
- String literals containing the name
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


# Pattern for valid Python identifiers
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class AstRenameTool(Tool):
    name = "ast_rename"
    description = (
        "Rename a Python symbol (function, class, variable, method) across "
        "the codebase using AST analysis. Only renames actual code identifiers — "
        "won't touch strings, comments, or unrelated scopes. Safer than "
        "find_replace for refactoring. "
        "Shows a preview by default (apply=false); set apply=true to write changes. "
        "IMPORTANT: always run with apply=false first to verify the rename scope is "
        "correct before applying. Use symbol_type='function' or 'class' instead of "
        "'any' when possible — 'any' matches all uses including attribute accesses, "
        "which can cause false positives for common names like 'run' or 'execute'."
    )
    requires_path_check = True
    tags = frozenset({"file_write"})

    MAX_FILES = 500

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "old_name": {
                    "type": "string",
                    "description": "Current symbol name (e.g. 'my_function', 'MyClass').",
                },
                "new_name": {
                    "type": "string",
                    "description": "New symbol name.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Directory or file to scan. "
                        "Examples: 'harness/' for a package, 'harness/tools/bash.py' for one file."
                    ),
                },
                "symbol_type": {
                    "type": "string",
                    "enum": ["function", "class", "variable", "method", "any"],
                    "description": (
                        "What kind of symbol to rename. 'any' matches all types. "
                        "'method' requires class_name parameter."
                    ),
                },
                "class_name": {
                    "type": "string",
                    "description": (
                        "For symbol_type='method': the class containing the method. "
                        "Ignored for other symbol types."
                    ),
                },
                "apply": {
                    "type": "boolean",
                    "description": (
                        "If true, write changes to disk. If false (default), "
                        "show a preview of what would change."
                    ),
                },
            },
            "required": ["old_name", "new_name", "scope", "symbol_type"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        old_name: str,
        new_name: str,
        scope: str,
        symbol_type: str = "any",
        class_name: str = "",
        apply: bool = False,
    ) -> ToolResult:
        # Validate names
        if not _IDENT_RE.match(old_name):
            return ToolResult(error=f"Invalid Python identifier: {old_name!r}", is_error=True)
        if not _IDENT_RE.match(new_name):
            return ToolResult(error=f"Invalid Python identifier: {new_name!r}", is_error=True)
        if old_name == new_name:
            return ToolResult(error="old_name and new_name are the same", is_error=True)
        if symbol_type == "method" and not class_name:
            return ToolResult(error="class_name is required for symbol_type='method'", is_error=True)

        # Resolve scope
        if os.path.isabs(scope):
            root = Path(scope)
        else:
            root = Path(config.workspace) / scope

        # Collect Python files
        if root.is_file():
            py_files = [root] if root.suffix == ".py" else []
        elif root.is_dir():
            py_files = self._collect_py_files(root)
        else:
            return ToolResult(error=f"Path not found: {root}", is_error=True)

        if not py_files:
            return ToolResult(output="No Python files found in scope")

        # Find and plan renames
        changes: list[dict[str, Any]] = []
        errors: list[str] = []

        for fp in sorted(py_files):
            try:
                source = fp.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{fp}: {exc}")
                continue

            try:
                tree = ast.parse(source, filename=str(fp))
            except SyntaxError as exc:
                errors.append(f"{fp}: SyntaxError: {exc}")
                continue

            file_changes = self._find_renames(
                tree, source, old_name, new_name, symbol_type, class_name
            )
            if file_changes:
                rel_path = str(fp.relative_to(Path(config.workspace)))
                changes.append({
                    "path": fp,
                    "rel_path": rel_path,
                    "source": source,
                    "renames": file_changes,
                })

        if not changes:
            return ToolResult(
                output=f"No occurrences of '{old_name}' ({symbol_type}) found in scope"
            )

        # Build preview
        total_renames = sum(len(c["renames"]) for c in changes)
        lines: list[str] = [
            f"{'Will rename' if apply else 'Preview'}: "
            f"'{old_name}' -> '{new_name}' ({symbol_type})",
            f"  {total_renames} occurrence(s) in {len(changes)} file(s)",
            "",
        ]

        for change in changes:
            lines.append(f"  {change['rel_path']}:")
            source_lines = change["source"].splitlines()
            for r in change["renames"][:20]:
                line_num = r["line"]
                context = source_lines[line_num - 1].strip() if line_num <= len(source_lines) else "?"
                lines.append(f"    line {line_num}: {r['kind']}  {context}")
            if len(change["renames"]) > 20:
                lines.append(f"    ... +{len(change['renames']) - 20} more")

        if errors:
            lines.append("")
            lines.append(f"  {len(errors)} file(s) skipped due to errors")

        # Apply changes if requested
        if apply:
            applied = 0
            for change in changes:
                new_source = self._apply_renames(change["source"], change["renames"], old_name, new_name)
                try:
                    change["path"].write_text(new_source, encoding="utf-8")
                    applied += 1
                except OSError as exc:
                    errors.append(f"Write failed: {change['path']}: {exc}")

            lines.append("")
            lines.append(f"Applied: {applied}/{len(changes)} file(s) updated")

        output = "\n".join(lines)
        return ToolResult(output=output)

    def _collect_py_files(self, root: Path) -> list[Path]:
        """Collect Python files, skipping hidden/venv/cache dirs."""
        result: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in ("__pycache__", "node_modules", ".git", ".venv", "venv", "env")
            ]
            for f in filenames:
                if f.endswith(".py"):
                    result.append(Path(dirpath) / f)
                    if len(result) >= self.MAX_FILES:
                        return result
        return result

    def _find_renames(
        self,
        tree: ast.Module,
        source: str,
        old_name: str,
        new_name: str,
        symbol_type: str,
        class_name: str,
    ) -> list[dict[str, Any]]:
        """Find all AST nodes that should be renamed."""
        renames: list[dict[str, Any]] = []

        # Build parent map for context
        parent_map: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent_map[id(child)] = node

        for node in ast.walk(tree):
            rename_info = self._check_node(
                node, parent_map, old_name, symbol_type, class_name
            )
            if rename_info:
                renames.append(rename_info)

        # Sort by line number (descending) for safe text replacement
        renames.sort(key=lambda r: (r["line"], r["col"]))
        return renames

    def _check_node(
        self,
        node: ast.AST,
        parent_map: dict[int, ast.AST],
        old_name: str,
        symbol_type: str,
        class_name: str,
    ) -> dict[str, Any] | None:
        """Check if a node is a rename target. Returns info dict or None."""

        # Function/method definition
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != old_name:
                return None
            parent = parent_map.get(id(node))
            if symbol_type == "method":
                if isinstance(parent, ast.ClassDef) and parent.name == class_name:
                    return {"line": node.lineno, "col": node.col_offset, "kind": "def", "end_col": node.col_offset + len(old_name)}
            elif symbol_type in ("function", "any"):
                if not isinstance(parent, ast.ClassDef):
                    return {"line": node.lineno, "col": node.col_offset, "kind": "def", "end_col": node.col_offset + len(old_name)}
                elif symbol_type == "any":
                    return {"line": node.lineno, "col": node.col_offset, "kind": "def", "end_col": node.col_offset + len(old_name)}
            return None

        # Class definition
        if isinstance(node, ast.ClassDef):
            if node.name == old_name and symbol_type in ("class", "any"):
                return {"line": node.lineno, "col": node.col_offset, "kind": "class-def", "end_col": node.col_offset + len(old_name)}
            return None

        # Name references (variables, function calls, etc.)
        if isinstance(node, ast.Name) and node.id == old_name:
            parent = parent_map.get(id(node))
            # Skip if this is part of a function/class def (handled above)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return None
            if symbol_type in ("function", "class", "variable", "any"):
                return {"line": node.lineno, "col": node.col_offset, "kind": "ref", "end_col": node.col_offset + len(old_name)}
            return None

        # Attribute access: obj.old_name (for method renames)
        if isinstance(node, ast.Attribute) and node.attr == old_name:
            if symbol_type in ("method", "any"):
                return {"line": node.lineno, "col": node.col_offset, "kind": "attr", "end_col": node.end_col_offset}
            return None

        # Import: from module import old_name
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == old_name and symbol_type in ("function", "class", "variable", "any"):
                    if alias.lineno is not None:
                        return {"line": alias.lineno, "col": alias.col_offset, "kind": "import", "end_col": alias.col_offset + len(old_name)}
            return None

        return None

    def _apply_renames(
        self,
        source: str,
        renames: list[dict[str, Any]],
        old_name: str,
        new_name: str,
    ) -> str:
        """Apply renames to source text using line/col positions.

        We work line-by-line and apply column-level replacements.
        Multiple renames on the same line are handled by processing
        right-to-left (highest column first) so earlier replacements
        don't shift later column positions.
        """
        lines = source.splitlines(keepends=True)

        # Group renames by line number
        by_line: dict[int, list[dict[str, Any]]] = {}
        for r in renames:
            by_line.setdefault(r["line"], []).append(r)

        for line_num, line_renames in by_line.items():
            if line_num < 1 or line_num > len(lines):
                continue
            line = lines[line_num - 1]
            # Process right-to-left to preserve column positions
            for r in sorted(line_renames, key=lambda x: x["col"], reverse=True):
                col = r["col"]
                # For attribute access, find the actual attribute position
                if r["kind"] == "attr":
                    # Find old_name after the dot on this line
                    attr_pos = line.find(f".{old_name}", col)
                    if attr_pos >= 0:
                        start = attr_pos + 1  # skip the dot
                        end = start + len(old_name)
                        line = line[:start] + new_name + line[end:]
                elif r["kind"] == "import":
                    # For imports, find the exact name in the import line
                    # Handle: from x import old_name, from x import old_name as alias
                    import_pos = line.find(old_name, col)
                    if import_pos >= 0:
                        end = import_pos + len(old_name)
                        line = line[:import_pos] + new_name + line[end:]
                elif r["kind"] == "def":
                    # For def/class, the name follows 'def ' or 'async def ' or 'class '
                    # Find the name token at approximately the right position
                    name_pos = line.find(old_name, col)
                    if name_pos >= 0:
                        end = name_pos + len(old_name)
                        line = line[:name_pos] + new_name + line[end:]
                elif r["kind"] == "class-def":
                    name_pos = line.find(old_name, col)
                    if name_pos >= 0:
                        end = name_pos + len(old_name)
                        line = line[:name_pos] + new_name + line[end:]
                else:
                    # ref — direct name reference
                    # Verify the exact text at this position
                    end = col + len(old_name)
                    if line[col:end] == old_name:
                        line = line[:col] + new_name + line[end:]
                    else:
                        # Fallback: find nearest occurrence
                        name_pos = line.find(old_name, max(0, col - 5))
                        if name_pos >= 0:
                            end = name_pos + len(old_name)
                            line = line[:name_pos] + new_name + line[end:]

            lines[line_num - 1] = line

        return "".join(lines)
