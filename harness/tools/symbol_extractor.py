"""symbol_extractor — extract named symbols from Python source files via AST.

Provides precise, token-efficient extraction of individual functions, classes,
and methods from Python source files without reading entire files.  This is
complementary to ``read_file`` (which operates on line ranges) and
``code_analysis`` (which builds a symbol table but does not return source).

Key capabilities
----------------
* **Single-symbol extraction**: given a file path and a symbol name like
  ``"MyClass"`` or ``"MyClass.my_method"``, returns the exact source text
  of that definition — no surrounding noise.
* **Multi-symbol extraction**: supply a list of names to fetch several
  symbols in one call (e.g. ``["Evaluator", "Evaluator.evaluate",
  "build_evaluator"]``).
* **Pattern matching**: glob-style patterns via ``fnmatch`` (e.g.
  ``"_check_*"`` matches all private helpers starting with ``_check_``).
* **Context lines**: optionally include N lines of context before the
  definition (useful to see the decorator or docstring framing).
* **Cross-file search**: when ``path`` is a directory, search all Python
  files under it for the named symbol (respects ``file_glob``).

Output format
-------------
Each matched symbol is rendered as::

    === path/to/file.py :: ClassName.method_name (line N) ===
    <source text of the definition>

When ``format="json"`` a structured list is returned instead.

Design principles
-----------------
* Pure stdlib — ``ast`` + ``textwrap`` only; no third-party dependencies.
* Never executes the target code — 100 % static analysis.
* Path-checked against ``config.allowed_paths`` like every other file tool.
* Async-safe: source reading is synchronous but fast (< 1 ms for typical
  files); the asyncio event loop is not blocked for any meaningful duration.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import textwrap
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _get_source_segment(source: str, node: ast.AST) -> str:
    """Return the exact source text for *node* using ast.get_source_segment.

    Falls back to a line-range slice when ``ast.get_source_segment`` returns
    ``None`` (e.g. on older Python versions or for synthetic nodes).
    """
    segment = ast.get_source_segment(source, node)
    if segment is not None:
        return segment

    # Fallback: slice by line numbers
    lines = source.splitlines(keepends=True)
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    return "".join(lines[start:end])


def _dedent_source(text: str) -> str:
    """Remove common leading whitespace from a multi-line source block."""
    return textwrap.dedent(text)


def _node_qualname(node: ast.AST, class_name: str | None = None) -> str:
    """Compute the qualified name for a function/class node."""
    name = getattr(node, "name", "")
    if class_name:
        return f"{class_name}.{name}"
    return name


# ---------------------------------------------------------------------------
# Core extraction: one file → list of SymbolMatch
# ---------------------------------------------------------------------------


class _SymbolMatch:
    """One resolved symbol from a source file."""

    __slots__ = ("qualname", "file", "lineno", "end_lineno", "source_text", "kind")

    def __init__(
        self,
        qualname: str,
        file: str,
        lineno: int,
        end_lineno: int,
        source_text: str,
        kind: str,  # "function", "async_function", "class", "method", "async_method"
    ) -> None:
        self.qualname = qualname
        self.file = file
        self.lineno = lineno
        self.end_lineno = end_lineno
        self.source_text = source_text
        self.kind = kind


def _extract_symbols_from_source(
    source: str,
    filepath: str,
    names: list[str],
) -> list[_SymbolMatch]:
    """Parse *source* and return all symbols whose names match *names*.

    Matching rules — for each pattern in *names*:

    * If the pattern contains a dot (e.g. ``"MyClass.method"``), it is matched
      against the *qualified* name ``"ClassName.method_name"``.
    * If the pattern contains no dot (e.g. ``"input_schema"`` or ``"_check_*"``),
      it is matched against **both** the bare symbol name *and* the method-name
      portion of a qualified name, so ``"input_schema"`` matches
      ``"ReadFileTool.input_schema"``, ``"WriteFileTool.input_schema"``, etc.
      This makes the tool much more ergonomic for searching across files.

    Glob patterns (``fnmatch`` syntax) are supported in both forms:
    ``"_check_*"`` matches all top-level helpers starting with ``_check_``, and
    ``"*.execute"`` matches the ``execute`` method on any class.

    Returns an empty list when the file has a syntax error (the caller logs
    and continues).
    """
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    matches: list[_SymbolMatch] = []

    # Pre-classify patterns: those with a dot are matched against the full
    # qualified name; those without a dot are matched against the leaf name
    # (the part after the last dot, or the whole name for top-level symbols).
    dotted_pats   = [p for p in names if "." in p]
    undotted_pats = [p for p in names if "." not in p]

    def _matches_qualified(qualname: str) -> bool:
        """Check full qualified name against dotted patterns."""
        return any(fnmatch.fnmatch(qualname, pat) for pat in dotted_pats)

    def _matches_leaf(leaf_name: str) -> bool:
        """Check the bare name (leaf) against undotted patterns."""
        return any(fnmatch.fnmatch(leaf_name, pat) for pat in undotted_pats)

    def _matches(qualname: str, leaf_name: str) -> bool:
        return _matches_qualified(qualname) or _matches_leaf(leaf_name)

    for node in ast.iter_child_nodes(tree):
        # ---- top-level function / async function ----
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = node.name
            if _matches(qname, qname):
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                text = _dedent_source(_get_source_segment(source, node))
                matches.append(
                    _SymbolMatch(
                        qualname=qname,
                        file=filepath,
                        lineno=node.lineno,
                        end_lineno=node.end_lineno or node.lineno,
                        source_text=text,
                        kind=kind,
                    )
                )

        # ---- top-level class ----
        elif isinstance(node, ast.ClassDef):
            class_name = node.name
            # Match the class itself (bare name, no dot)
            if _matches(class_name, class_name):
                text = _dedent_source(_get_source_segment(source, node))
                matches.append(
                    _SymbolMatch(
                        qualname=class_name,
                        file=filepath,
                        lineno=node.lineno,
                        end_lineno=node.end_lineno or node.lineno,
                        source_text=text,
                        kind="class",
                    )
                )

            # Match individual methods: check both qualified ("Class.meth") and
            # the bare method name ("meth") against the respective pattern sets.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_qname = f"{class_name}.{child.name}"
                    if _matches(method_qname, child.name):
                        kind = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                        text = _dedent_source(_get_source_segment(source, child))
                        matches.append(
                            _SymbolMatch(
                                qualname=method_qname,
                                file=filepath,
                                lineno=child.lineno,
                                end_lineno=child.end_lineno or child.lineno,
                                source_text=text,
                                kind=kind,
                            )
                        )

    return matches


# ---------------------------------------------------------------------------
# Context-lines helper
# ---------------------------------------------------------------------------


def _add_context_before(
    source_text: str,
    lineno: int,
    file_source: str,
    context_lines: int,
) -> str:
    """Prepend up to *context_lines* lines before the symbol definition.

    This is useful to see decorators (``@property``, ``@staticmethod``) or
    a preceding comment block that gives semantic context.
    """
    if context_lines <= 0:
        return source_text
    all_lines = file_source.splitlines(keepends=True)
    start_idx = max(0, lineno - 1 - context_lines)
    end_idx = lineno - 1  # exclusive — symbol itself starts here
    prefix = "".join(all_lines[start_idx:end_idx])
    if prefix.strip():
        return prefix + source_text
    return source_text


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------


def _format_matches_text(matches: list[_SymbolMatch]) -> str:
    """Render a list of SymbolMatch instances as a human-readable text block."""
    if not matches:
        return "(no symbols found)"
    parts: list[str] = []
    for m in matches:
        header = (
            f"=== {m.file} :: {m.qualname}  [{m.kind}]  "
            f"(line {m.lineno}–{m.end_lineno}) ==="
        )
        parts.append(header)
        parts.append(m.source_text.rstrip())
        parts.append("")  # blank line separator
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class SymbolExtractorTool(Tool):
    """Extract the complete source of named Python symbols via AST.

    Unlike ``read_file`` (line-range based) or ``code_analysis`` (symbol
    table only), this tool returns the **exact source text** of a named
    function, class, or method — nothing more, nothing less.

    This is especially useful when:

    * You need to read a large file but only care about one or two functions.
    * You want to verify the current implementation of a specific method
      before editing it (more precise than read_file + grep).
    * You are generating a patch and need the exact current text to compute
      the ``old_str`` for ``edit_file``.

    Symbol names
    ------------
    * ``"MyClass"``                — entire class body
    * ``"MyClass.my_method"``      — single method
    * ``"my_function"``            — top-level function
    * ``"_helper_*"``              — all top-level names matching the glob
    * ``["ClassA", "ClassB.run"]`` — multiple symbols in one call

    Cross-file search
    -----------------
    Set ``path`` to a directory to search all matching Python files for the
    symbol.  The ``file_glob`` parameter (default ``**/*.py``) controls which
    files are searched.

    Output
    ------
    Text (default): one fenced block per symbol with a header line showing
    the file, qualified name, kind, and line range.

    JSON (``format="json"``): a list of objects with keys ``qualname``,
    ``file``, ``lineno``, ``end_lineno``, ``kind``, ``source``.
    """

    name = "symbol_extractor"
    description = (
        "Extract the complete source of named Python functions, classes, or methods "
        "using AST — no need to know line numbers. "
        "Supply a symbol name like 'MyClass', 'MyClass.method', or a glob pattern "
        "like '_check_*'. Can search a single file or a whole directory. "
        "Much more token-efficient than read_file when you only need one function. "
        "Does not execute any code."
    )
    requires_path_check = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search. When a directory, all "
                        "Python files matching file_glob are searched."
                    ),
                },
                "symbols": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Symbol name(s) to extract. May be a plain name "
                        "('MyClass'), a qualified name ('MyClass.method'), "
                        "a glob pattern ('_helper_*'), or a list of any of "
                        "the above. Glob patterns use fnmatch syntax."
                    ),
                },
                "file_glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern when path is a directory (default: **/*.py). "
                        "E.g. 'harness/*.py' to restrict to one package."
                    ),
                    "default": "**/*.py",
                },
                "context_lines": {
                    "type": "integer",
                    "description": (
                        "Number of lines to include before each symbol definition "
                        "(useful to capture decorators). Default: 0."
                    ),
                    "default": 0,
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "Output format: 'text' (default) or 'json'.",
                    "default": "text",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of symbols to return (default: 20). "
                        "Prevents accidental giant outputs when a broad glob "
                        "matches hundreds of symbols."
                    ),
                    "default": 20,
                },
            },
            "required": ["path", "symbols"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        path: str,
        symbols: str | list[str],
        file_glob: str = "**/*.py",
        context_lines: int = 0,
        format: str = "text",  # noqa: A002
        limit: int = 20,
    ) -> ToolResult:
        # Normalise symbols to a list of non-empty strings
        if isinstance(symbols, str):
            names: list[str] = [s.strip() for s in symbols.split(",") if s.strip()]
        else:
            names = [str(s).strip() for s in symbols if str(s).strip()]

        if not names:
            return ToolResult(
                error="symbols must not be empty — supply at least one name or pattern",
                is_error=True,
            )

        # Resolve and path-check
        resolved = str(Path(path).resolve())
        if err := self._check_path(config, resolved):
            return err

        p = Path(resolved)

        # Collect target files
        if p.is_file():
            if p.suffix != ".py":
                return ToolResult(
                    error=f"Not a Python file: {resolved}",
                    is_error=True,
                )
            files: list[Path] = [p]
        elif p.is_dir():
            files = sorted(
                (f for f in p.glob(file_glob) if f.is_file() and f.suffix == ".py"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            if not files:
                return ToolResult(
                    output=f"No Python files matched '{file_glob}' in {resolved}"
                )
        else:
            return ToolResult(error=f"Path not found: {resolved}", is_error=True)

        # Extract symbols from each file
        all_matches: list[_SymbolMatch] = []
        syntax_errors: list[str] = []

        for fpath in files:
            if len(all_matches) >= limit:
                break
            try:
                source = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                syntax_errors.append(f"{fpath}: {exc}")
                continue

            # Compute display path: relative to the resolved root (file or dir parent)
            display_root = p if p.is_dir() else p.parent
            try:
                display_path = str(fpath.relative_to(display_root))
            except ValueError:
                display_path = str(fpath)

            file_matches = _extract_symbols_from_source(source, display_path, names)
            if not file_matches and p.is_file():
                # Single-file mode with no syntax error means names simply not found
                syntax_errors.append(
                    f"No symbols matching {names!r} found in {display_path}"
                )

            # Apply context lines
            if context_lines > 0:
                for m in file_matches:
                    m.source_text = _add_context_before(
                        m.source_text, m.lineno, source, context_lines
                    )

            all_matches.extend(file_matches)

        # Apply global limit
        truncated = len(all_matches) > limit
        all_matches = all_matches[:limit]

        # ---- format output ----
        if format == "json":
            data = [
                {
                    "qualname": m.qualname,
                    "file": m.file,
                    "lineno": m.lineno,
                    "end_lineno": m.end_lineno,
                    "kind": m.kind,
                    "source": m.source_text,
                }
                for m in all_matches
            ]
            output = json.dumps(data, indent=2, ensure_ascii=False)
            if truncated:
                output += f"\n// ... (truncated to {limit} symbols)"
            return ToolResult(output=output)

        # text format
        if not all_matches:
            msg_parts = [f"No symbols matching {names!r} found"]
            if p.is_dir():
                msg_parts.append(f"in {len(files)} Python file(s) under {resolved}")
            else:
                msg_parts.append(f"in {resolved}")
            if syntax_errors:
                msg_parts.append(f"\nNotes:\n" + "\n".join(f"  {e}" for e in syntax_errors[:5]))
            return ToolResult(output=" ".join(msg_parts[:2]) + (
                "\n" + "\n".join(msg_parts[2:]) if len(msg_parts) > 2 else ""
            ))

        header_parts = [
            f"Found {len(all_matches)} symbol(s) matching {names!r}"
        ]
        if truncated:
            header_parts.append(f"(showing first {limit} — increase limit to see more)")
        if p.is_dir():
            header_parts.append(f"across {len(files)} file(s)")

        output_lines = [" ".join(header_parts), ""]
        output_lines.append(_format_matches_text(all_matches))

        if syntax_errors and p.is_dir():
            # In directory mode, only show errors if *no* matches were found in
            # certain files — otherwise it's just noise
            pass  # suppress per-file "not found" notes in directory scan

        return ToolResult(output="\n".join(output_lines))
