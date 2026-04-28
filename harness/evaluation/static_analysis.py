"""static_analysis — lightweight objective code-quality checks for the Evaluator.

Performs deterministic, non-LLM checks on a set of Python source files and
returns a structured report that the Evaluator injects into its prompt.  This
grounds the LLM verdict in *facts* (syntax errors, undefined imports, missing
symbols) rather than pure opinion.

Checks performed
----------------
1. **Syntax validity** — ``py_compile`` on every ``.py`` file in the changed
   set.  A file that fails ``py_compile`` cannot be imported; the evaluator
   should always FAIL on a syntax error unless the task was explicitly to *add*
   a broken file (rare).

2. **Import sanity** — ``ast.parse`` every changed file, collect all
   ``import X`` and ``from X import Y`` statements, and flag imports of
   modules that do *not* exist in the current Python environment (stdlib,
   installed packages, or a sibling file in the workspace).  Unknown imports
   are a soft warning — the task may be adding a new dependency — so they do
   not force a FAIL but are surfaced as ``WARN`` findings.

3. **Symbol existence** — for every ``from X import Y``, verify that symbol
   ``Y`` is actually exported by module ``X`` when that module is reachable
   and parseable.  A missing symbol (e.g. ``from harness.core.llm import LLMCLient``
   when the class is spelled ``LLM``) is reported as an ``ERROR`` finding.

4. **Structural diff** — compare the *set of top-level class and function
   names* declared in each changed file against the set that existed before
   execution (read from the execution log's ``read_file`` outputs).  Any
   name that vanished and is not listed in ``files_changed`` as deleted is
   reported as a potential regression.

All checks run synchronously in the calling thread (no subprocess, no I/O
beyond reading files already on disk).  The total runtime is typically < 50 ms
for a typical harness task touching < 20 files.

Integration
-----------
``Evaluator.evaluate()`` calls ``run_static_checks(files_changed, workspace)``
and prepends the resulting ``StaticReport`` to the LLM user message.  The
prompts already contain instructions for the conservative reviewer to treat
``ERROR`` findings as automatic FAIL items.

Usage::

    from harness.evaluation.static_analysis import run_static_checks

    report = run_static_checks(result.files_changed, config.workspace)
    print(report.summary)          # "2 errors, 1 warning, 3 ok"
    print(report.has_errors)       # True
    print(report.to_prompt_block()) # markdown block for LLM injection
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import py_compile
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_LEVEL_ERROR = "ERROR"
_LEVEL_WARN  = "WARN"
_LEVEL_INFO  = "INFO"


@dataclass
class Finding:
    """One static-analysis finding."""

    level: str        # "ERROR", "WARN", "INFO"
    file: str         # relative or absolute path
    message: str      # human-readable description
    line: int = 0     # 0 = not applicable


@dataclass
class StaticReport:
    """Aggregate result of all static checks on the changed files."""

    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0
    files_skipped: int = 0   # non-Python or unreadable

    # ---- computed properties ----

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == _LEVEL_ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == _LEVEL_WARN]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def summary(self) -> str:
        e, w = len(self.errors), len(self.warnings)
        ok = self.files_checked - len({f.file for f in self.findings if f.level == _LEVEL_ERROR})
        return (
            f"{e} error(s), {w} warning(s), {ok} file(s) clean "
            f"[{self.files_checked} checked, {self.files_skipped} skipped]"
        )

    def to_prompt_block(self) -> str:
        """Return a compact markdown block for injection into the evaluator prompt.

        The block starts with a one-line summary; if there are findings they
        are listed as a table so the LLM can parse them without confusion.
        Returns an empty string when there are no changed Python files.
        """
        if self.files_checked == 0:
            return ""

        lines: list[str] = [
            "## Static Analysis Results",
            "",
            f"**{self.summary}**",
            "",
        ]

        if not self.findings:
            lines.append("All changed Python files passed static analysis. ✓")
            return "\n".join(lines)

        lines.append("| Level | File | Line | Finding |")
        lines.append("|-------|------|------|---------|")
        for f in self.findings:
            line_str = str(f.line) if f.line else "—"
            # Escape pipe characters inside cells
            msg = f.message.replace("|", "\\|")
            fname = f.file.replace("|", "\\|")
            lines.append(f"| {f.level} | `{fname}` | {line_str} | {msg} |")

        lines.append("")
        if self.has_errors:
            lines.append(
                "**⚠ ERROR findings indicate objective defects that must be fixed "
                "regardless of LLM opinion.  The conservative reviewer MUST FAIL "
                "this execution if any ERROR findings are present.**"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Check 1: syntax validity via py_compile
# ---------------------------------------------------------------------------


def _check_syntax(path: Path, rel: str) -> list[Finding]:
    """Run py_compile on *path*; return a Finding on failure, else []."""
    try:
        py_compile.compile(str(path), doraise=True)
        return []
    except py_compile.PyCompileError as exc:
        # exc.msg includes "filename:lineno: SyntaxError: ..."
        msg = str(exc.msg).strip()
        # Try to extract line number from the message.
        # py_compile messages may use either "filename:lineno:" or ", line N"
        line = 0
        m = re.search(r", line (\d+)", msg) or re.search(r":(\d+):", msg)
        if m:
            line = int(m.group(1))
        return [Finding(level=_LEVEL_ERROR, file=rel, message=f"syntax error: {msg}", line=line)]
    except Exception as exc:
        return [Finding(level=_LEVEL_WARN, file=rel, message=f"py_compile raised {type(exc).__name__}: {exc}")]


# ---------------------------------------------------------------------------
# Check 2: import sanity + Check 3: symbol existence
# ---------------------------------------------------------------------------


def _get_top_level_names(source: str) -> set[str]:
    """Return the set of top-level class and function names defined in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _module_file_in_workspace(module: str, workspace: Path) -> Path | None:
    """Try to locate ``module`` as a .py file relative to *workspace*.

    Handles dotted names: ``harness.llm`` → ``workspace/harness/llm.py``.
    Returns the Path if found, else None.
    """
    parts = module.split(".")
    candidate = workspace.joinpath(*parts).with_suffix(".py")
    if candidate.is_file():
        return candidate
    # Also try as a package __init__.py
    init = workspace.joinpath(*parts, "__init__.py")
    if init.is_file():
        return init
    return None


def _is_stdlib_or_installed(module: str) -> bool:
    """Return True if the top-level module is importable in the current env."""
    top = module.split(".")[0]
    # Fast path: already cached in sys.modules
    if top in sys.modules:
        return True
    # Check without actually importing — uses the finder machinery
    try:
        spec = importlib.util.find_spec(top)
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _check_imports(
    source: str,
    rel: str,
    workspace: Path,
) -> list[Finding]:
    """Parse imports in *source* and return findings for suspicious ones.

    Check 2: flag unknown top-level modules as WARN.
    Check 3: for ``from X import Y``, verify Y is exported by X (ERROR when
    the module is in-workspace and parseable but Y is absent).
    """
    findings: list[Finding] = []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # syntax errors handled by check_syntax; don't double-report

    for node in ast.walk(tree):
        # ---- plain `import X` or `import X as Y` ----
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if not _is_stdlib_or_installed(mod):
                    if _module_file_in_workspace(mod, workspace) is None:
                        findings.append(Finding(
                            level=_LEVEL_WARN,
                            file=rel,
                            line=getattr(node, "lineno", 0),
                            message=f"import {mod!r} — module not found in stdlib, site-packages, or workspace",
                        ))

        # ---- `from X import Y, Z` ----
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not mod:
                continue  # relative import with no module — skip

            # Determine whether we can resolve the module at all
            in_workspace = _module_file_in_workspace(mod, workspace)
            in_env = _is_stdlib_or_installed(mod)

            if not in_workspace and not in_env:
                findings.append(Finding(
                    level=_LEVEL_WARN,
                    file=rel,
                    line=getattr(node, "lineno", 0),
                    message=f"from {mod!r} import … — module not found in stdlib, site-packages, or workspace",
                ))
                continue

            # Module is reachable — check that each imported symbol exists
            if in_workspace:
                try:
                    mod_source = in_workspace.read_text(encoding="utf-8", errors="replace")
                    exported = _get_top_level_names(mod_source)
                    # Also include __all__ if present (best-effort)
                    # and module-level variables / constants
                    mod_tree = ast.parse(mod_source)
                    for mn in ast.iter_child_nodes(mod_tree):
                        if isinstance(mn, (ast.Assign, ast.AnnAssign)):
                            if isinstance(mn, ast.Assign):
                                for t in mn.targets:
                                    if isinstance(t, ast.Name):
                                        exported.add(t.id)
                            elif isinstance(mn, ast.AnnAssign) and isinstance(mn.target, ast.Name):
                                exported.add(mn.target.id)
                    # Also include names re-exported via 'from X import Y' inside
                    # the module (very common in __init__.py files).
                    for mn in ast.iter_child_nodes(mod_tree):
                        if isinstance(mn, ast.ImportFrom) and mn.module:
                            for alias in mn.names:
                                exported.add(alias.asname or alias.name)
                        elif isinstance(mn, ast.Import):
                            for alias in mn.names:
                                # `import X` at module level makes X available
                                exported.add(alias.asname or alias.name.split(".")[0])

                    # Determine the parent directory of the module file so we can
                    # check whether an imported name is a sibling sub-module/package.
                    mod_parent_dir = in_workspace.parent

                    for alias in node.names:
                        name = alias.name
                        if name == "*":
                            continue  # star imports: can't check statically
                        if name in exported:
                            continue  # found as an explicit symbol

                        # Check whether 'name' is a sub-module of mod (e.g.
                        # `from harness.prompts import evaluator` where evaluator
                        # is harness/prompts/evaluator.py).  This covers the very
                        # common pattern of importing sibling modules via a package.
                        # NOTE: only applicable when `mod` resolves to a *package*
                        # __init__.py (a directory), not a plain .py leaf file.
                        # For leaf files (harness/llm.py), `name` must be an
                        # explicit symbol — there are no sub-modules to import.
                        mod_is_package = in_workspace.name == "__init__.py"
                        if mod_is_package:
                            submod_file = mod_parent_dir / f"{name}.py"
                            submod_pkg  = mod_parent_dir / name / "__init__.py"
                            if submod_file.exists() or submod_pkg.exists():
                                continue  # it's a sub-module — perfectly valid

                            # Also check stdlib/installed for the fully-qualified name,
                            # but only when the parent package is NOT in-workspace —
                            # otherwise harness.llm.NonExistentClass would incorrectly
                            # pass because `harness` is importable.
                            if not _module_file_in_workspace(mod, workspace):
                                if _is_stdlib_or_installed(f"{mod}.{name}"):
                                    continue

                        findings.append(Finding(
                            level=_LEVEL_ERROR,
                            file=rel,
                            line=getattr(node, "lineno", 0),
                            message=(
                                f"from {mod!r} import {name!r} — "
                                f"symbol {name!r} not found in {in_workspace.name} "
                                f"(exported: {sorted(exported)[:8]}{'…' if len(exported) > 8 else ''})"
                            ),
                        ))
                except OSError:
                    pass  # can't read the module file — skip symbol check

    return findings


# ---------------------------------------------------------------------------
# Check 4: structural regression (disappearing top-level names)
# ---------------------------------------------------------------------------


def _check_structural_regression(
    path: Path,
    rel: str,
    before_source: str | None,
) -> list[Finding]:
    """Compare top-level names before and after; warn on removed names.

    Only applicable when we have a pre-execution snapshot of the file
    (supplied via the ``before_source`` argument; None = file was new).
    """
    if before_source is None:
        return []  # new file — nothing to compare

    try:
        after_source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    before_names = _get_top_level_names(before_source)
    after_names  = _get_top_level_names(after_source)
    removed = before_names - after_names

    findings: list[Finding] = []
    for name in sorted(removed):
        findings.append(Finding(
            level=_LEVEL_WARN,
            file=rel,
            line=0,
            message=(
                f"Top-level name {name!r} existed before execution but is now absent "
                f"— potential regression if callers depend on it"
            ),
        ))
    return findings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_static_checks(
    files_changed: list[str],
    workspace: str,
    *,
    before_snapshots: dict[str, str] | None = None,
) -> StaticReport:
    """Run all static checks on *files_changed* and return a StaticReport.

    Args:
        files_changed:    List of file paths (absolute or relative) that were
                          written/edited during execution.  Non-``.py`` files
                          are counted as skipped.
        workspace:        Absolute path to the project root.  Used to resolve
                          intra-project imports.
        before_snapshots: Optional mapping of ``rel_path → source_text`` for
                          the state of files *before* execution (enables check
                          4, structural regression detection).  Pass ``None``
                          to skip structural checks.

    Returns:
        A :class:`StaticReport` with all findings.
    """
    ws = Path(workspace)
    report = StaticReport()
    snapshots = before_snapshots or {}

    for raw_path in files_changed:
        p = Path(raw_path)
        if not p.is_absolute():
            p = (ws / p).resolve()
        else:
            p = p.resolve()

        # Compute relative path for display
        try:
            rel = str(p.relative_to(ws))
        except ValueError:
            rel = str(p)

        # Skip non-Python and missing files
        if not p.exists() or not p.is_file():
            log.debug("static_analysis: skipping missing file %s", rel)
            report.files_skipped += 1
            continue
        if p.suffix != ".py":
            log.debug("static_analysis: skipping non-Python file %s", rel)
            report.files_skipped += 1
            continue

        report.files_checked += 1

        # Read current content once for reuse
        try:
            current_source = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report.findings.append(Finding(
                level=_LEVEL_WARN, file=rel, message=f"Could not read file: {exc}"
            ))
            continue

        # Check 1: syntax
        syntax_findings = _check_syntax(p, rel)
        report.findings.extend(syntax_findings)

        # Checks 2+3 only make sense when syntax is valid
        if not syntax_findings:
            report.findings.extend(_check_imports(current_source, rel, ws))

        # Check 4: structural regression
        before = snapshots.get(rel) or snapshots.get(str(p))
        report.findings.extend(_check_structural_regression(p, rel, before))

    log.info(
        "static_analysis: checked=%d skipped=%d errors=%d warnings=%d",
        report.files_checked,
        report.files_skipped,
        len(report.errors),
        len(report.warnings),
    )
    return report
