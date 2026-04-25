"""Tests for harness/evaluation/static_analysis.py

Covers all four static checks:
1. Syntax validity (py_compile)
2. Import sanity (unknown module → WARN)
3. Symbol existence (from X import Y where Y missing → ERROR)
4. Structural regression (top-level name removed → WARN)

Also covers: StaticReport, Finding, to_prompt_block(), summary, edge cases.
"""
from __future__ import annotations

import textwrap

from harness.evaluation.static_analysis import (
    Finding,
    StaticReport,
    _check_imports,
    _check_structural_regression,
    _check_syntax,
    _get_top_level_names,
    _is_stdlib_or_installed,
    _module_file_in_workspace,
    run_static_checks,
)


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------

class TestFinding:
    def test_default_line_is_zero(self):
        f = Finding(level="ERROR", file="foo.py", message="oops")
        assert f.line == 0

    def test_explicit_line(self):
        f = Finding(level="WARN", file="bar.py", message="hmm", line=42)
        assert f.line == 42

    def test_level_stored(self):
        f = Finding(level="INFO", file="x.py", message="note")
        assert f.level == "INFO"


# ---------------------------------------------------------------------------
# StaticReport properties
# ---------------------------------------------------------------------------

class TestStaticReport:
    def _report_with(self, *findings: Finding) -> StaticReport:
        r = StaticReport()
        r.files_checked = len(findings) or 1
        r.findings.extend(findings)
        return r

    def test_errors_filtered(self):
        r = self._report_with(
            Finding("ERROR", "a.py", "bad"),
            Finding("WARN", "b.py", "hmm"),
            Finding("ERROR", "c.py", "also bad"),
        )
        assert len(r.errors) == 2
        assert all(f.level == "ERROR" for f in r.errors)

    def test_warnings_filtered(self):
        r = self._report_with(
            Finding("ERROR", "a.py", "bad"),
            Finding("WARN", "b.py", "hmm"),
        )
        assert len(r.warnings) == 1
        assert r.warnings[0].file == "b.py"

    def test_has_errors_true(self):
        r = self._report_with(Finding("ERROR", "a.py", "x"))
        assert r.has_errors is True

    def test_has_errors_false_with_only_warnings(self):
        r = self._report_with(Finding("WARN", "a.py", "x"))
        assert r.has_errors is False

    def test_has_errors_false_empty(self):
        r = StaticReport()
        assert r.has_errors is False

    def test_summary_no_findings(self):
        r = StaticReport()
        r.files_checked = 3
        r.files_skipped = 1
        s = r.summary
        assert "0 error" in s
        assert "0 warning" in s
        assert "3 checked" in s
        assert "1 skipped" in s

    def test_summary_with_findings(self):
        r = StaticReport()
        r.files_checked = 3
        r.findings.extend([
            Finding("ERROR", "a.py", "bad"),
            Finding("WARN", "b.py", "hmm"),
        ])
        s = r.summary
        assert "1 error" in s
        assert "1 warning" in s


# ---------------------------------------------------------------------------
# to_prompt_block()
# ---------------------------------------------------------------------------

class TestToPromptBlock:
    def test_empty_when_no_files_checked(self):
        r = StaticReport()
        r.files_checked = 0
        assert r.to_prompt_block() == ""

    def test_heading_present(self):
        r = StaticReport()
        r.files_checked = 1
        block = r.to_prompt_block()
        assert "## Static Analysis Results" in block

    def test_clean_files_message(self):
        r = StaticReport()
        r.files_checked = 2
        block = r.to_prompt_block()
        assert "passed static analysis" in block

    def test_findings_produce_table(self):
        r = StaticReport()
        r.files_checked = 1
        r.findings.append(Finding("ERROR", "foo.py", "syntax error", line=5))
        block = r.to_prompt_block()
        assert "| Level | File | Line | Finding |" in block
        assert "ERROR" in block
        assert "foo.py" in block
        assert "syntax error" in block
        assert "5" in block

    def test_dash_when_line_zero(self):
        r = StaticReport()
        r.files_checked = 1
        r.findings.append(Finding("WARN", "foo.py", "unknown module", line=0))
        block = r.to_prompt_block()
        assert "\u2014" in block

    def test_error_warning_text_injected(self):
        r = StaticReport()
        r.files_checked = 1
        r.findings.append(Finding("ERROR", "a.py", "bad"))
        block = r.to_prompt_block()
        assert "ERROR" in block

    def test_pipe_escaped_in_message(self):
        r = StaticReport()
        r.files_checked = 1
        r.findings.append(Finding("WARN", "a.py", "foo | bar"))
        block = r.to_prompt_block()
        assert "foo \\| bar" in block


# ---------------------------------------------------------------------------
# _get_top_level_names
# ---------------------------------------------------------------------------

class TestGetTopLevelNames:
    def test_function(self):
        src = "def foo(): pass"
        assert "foo" in _get_top_level_names(src)

    def test_async_function(self):
        src = "async def bar(): pass"
        assert "bar" in _get_top_level_names(src)

    def test_class(self):
        src = "class Baz: pass"
        assert "Baz" in _get_top_level_names(src)

    def test_nested_not_included(self):
        src = textwrap.dedent("""
            def outer():
                def inner():
                    pass
        """)
        names = _get_top_level_names(src)
        assert "outer" in names
        assert "inner" not in names

    def test_empty_source(self):
        assert _get_top_level_names("") == set()

    def test_syntax_error_returns_empty(self):
        assert _get_top_level_names("def (broken:") == set()

    def test_multiple_names(self):
        src = textwrap.dedent("""
            def foo(): pass
            class Bar: pass
            async def baz(): pass
        """)
        names = _get_top_level_names(src)
        assert names == {"foo", "Bar", "baz"}


# ---------------------------------------------------------------------------
# _check_syntax
# ---------------------------------------------------------------------------

class TestCheckSyntax:
    def test_valid_file(self, tmp_path):
        p = tmp_path / "ok.py"
        p.write_text("x = 1\n")
        findings = _check_syntax(p, "ok.py")
        assert findings == []

    def test_syntax_error_gives_error_finding(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def (broken:\n")
        findings = _check_syntax(p, "bad.py")
        assert len(findings) == 1
        assert findings[0].level == "ERROR"
        assert "syntax error" in findings[0].message.lower()

    def test_syntax_error_has_line_number(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("x = 1\ndef broken(\n    pass\n")
        findings = _check_syntax(p, "bad.py")
        # May or may not extract a line number, but if it does it should be > 0
        if findings and findings[0].line:
            assert findings[0].line > 0

    def test_syntax_error_rel_path_recorded(self, tmp_path):
        p = tmp_path / "sub" / "bad.py"
        p.parent.mkdir()
        p.write_text("def (:\n")
        findings = _check_syntax(p, "sub/bad.py")
        assert findings[0].file == "sub/bad.py"


# ---------------------------------------------------------------------------
# _check_imports (check 2 + check 3)
# ---------------------------------------------------------------------------

class TestCheckImports:
    def test_stdlib_import_no_findings(self, tmp_path):
        src = "import os\nimport sys\n"
        findings = _check_imports(src, "a.py", tmp_path)
        assert findings == []

    def test_unknown_module_gives_warn(self, tmp_path):
        src = "import totally_nonexistent_module_xyz123\n"
        findings = _check_imports(src, "a.py", tmp_path)
        warns = [f for f in findings if f.level == "WARN"]
        assert any("totally_nonexistent_module_xyz123" in f.message for f in warns)

    def test_from_stdlib_import_no_findings(self, tmp_path):
        src = "from pathlib import Path\n"
        findings = _check_imports(src, "a.py", tmp_path)
        assert findings == []

    def test_from_unknown_module_gives_warn(self, tmp_path):
        src = "from nonexistent_pkg_abc456 import something\n"
        findings = _check_imports(src, "a.py", tmp_path)
        warns = [f for f in findings if f.level == "WARN"]
        assert any("nonexistent_pkg_abc456" in f.message for f in warns)

    def test_from_workspace_module_valid_symbol(self, tmp_path):
        # Create a module in workspace with a known symbol
        (tmp_path / "mymod.py").write_text("def good_func(): pass\nMY_CONST = 1\n")
        src = "from mymod import good_func\n"
        findings = _check_imports(src, "user.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == []

    def test_from_workspace_module_missing_symbol_gives_error(self, tmp_path):
        # Create a module with known symbols
        (tmp_path / "mymod.py").write_text("def real_func(): pass\n")
        src = "from mymod import ghost_func\n"
        findings = _check_imports(src, "user.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert len(errors) == 1
        assert "ghost_func" in errors[0].message

    def test_star_import_no_error(self, tmp_path):
        # Star imports cannot be checked statically
        (tmp_path / "mymod.py").write_text("x = 1\n")
        src = "from mymod import *\n"
        findings = _check_imports(src, "user.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == []

    def test_relative_import_skipped(self, tmp_path):
        # `from . import something` — node.module is "" or None
        src = "from . import something\n"
        # Should not crash and should produce no error for the relative import
        findings = _check_imports(src, "a.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == []

    def test_syntax_error_source_returns_empty(self, tmp_path):
        # _check_imports should handle unparseable source gracefully
        findings = _check_imports("def broken(:\n", "a.py", tmp_path)
        assert findings == []

    def test_from_package_init_submodule_import(self, tmp_path):
        # Create a package
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "sub.py").write_text("def helper(): pass\n")
        # `from mypkg import sub` should resolve to sub.py — not an error
        src = "from mypkg import sub\n"
        findings = _check_imports(src, "user.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == []

    def test_workspace_module_constant_is_valid_symbol(self, tmp_path):
        (tmp_path / "cfg.py").write_text("MAX_RETRIES = 3\n")
        src = "from cfg import MAX_RETRIES\n"
        findings = _check_imports(src, "user.py", tmp_path)
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == []


# ---------------------------------------------------------------------------
# _check_structural_regression
# ---------------------------------------------------------------------------

class TestCheckStructuralRegression:
    def test_no_before_source_no_findings(self, tmp_path):
        p = tmp_path / "new.py"
        p.write_text("def fresh(): pass\n")
        findings = _check_structural_regression(p, "new.py", before_source=None)
        assert findings == []

    def test_no_regression_when_same_names(self, tmp_path):
        before = "def foo(): pass\n"
        after = "def foo(): pass\ndef bar(): pass\n"
        p = tmp_path / "a.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "a.py", before_source=before)
        assert findings == []

    def test_removed_function_gives_warn(self, tmp_path):
        before = "def important(): pass\ndef other(): pass\n"
        after = "def other(): pass\n"
        p = tmp_path / "a.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "a.py", before_source=before)
        warns = [f for f in findings if f.level == "WARN"]
        assert len(warns) == 1
        assert "important" in warns[0].message

    def test_removed_class_gives_warn(self, tmp_path):
        before = "class Foo: pass\n"
        after = "x = 1\n"
        p = tmp_path / "a.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "a.py", before_source=before)
        warns = [f for f in findings if f.level == "WARN"]
        assert any("Foo" in f.message for f in warns)

    def test_multiple_removals_reported(self, tmp_path):
        before = "def a(): pass\ndef b(): pass\ndef c(): pass\n"
        after = "def a(): pass\n"
        p = tmp_path / "a.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "a.py", before_source=before)
        warns = [f for f in findings if f.level == "WARN"]
        assert len(warns) == 2

    def test_missing_after_file_no_crash(self, tmp_path):
        p = tmp_path / "gone.py"
        # file does not exist — should return empty
        findings = _check_structural_regression(p, "gone.py", before_source="def x(): pass\n")
        assert findings == []


# ---------------------------------------------------------------------------
# run_static_checks (public API)
# ---------------------------------------------------------------------------

class TestRunStaticChecks:
    def test_empty_file_list(self, tmp_path):
        report = run_static_checks([], str(tmp_path))
        assert report.files_checked == 0
        assert report.files_skipped == 0
        assert report.findings == []

    def test_non_py_file_is_skipped(self, tmp_path):
        txt = tmp_path / "readme.txt"
        txt.write_text("hello")
        report = run_static_checks([str(txt)], str(tmp_path))
        assert report.files_skipped == 1
        assert report.files_checked == 0

    def test_missing_file_is_skipped(self, tmp_path):
        report = run_static_checks([str(tmp_path / "ghost.py")], str(tmp_path))
        assert report.files_skipped == 1
        assert report.files_checked == 0

    def test_valid_py_file_checked_no_findings(self, tmp_path):
        p = tmp_path / "ok.py"
        p.write_text("x = 1\nimport os\n")
        report = run_static_checks([str(p)], str(tmp_path))
        assert report.files_checked == 1
        assert report.files_skipped == 0
        assert not report.has_errors

    def test_syntax_error_detected(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def broken(:\n")
        report = run_static_checks([str(p)], str(tmp_path))
        assert report.files_checked == 1
        assert report.has_errors
        assert any("syntax" in f.message.lower() for f in report.errors)

    def test_relative_path_resolved(self, tmp_path):
        p = tmp_path / "rel.py"
        p.write_text("x = 1\n")
        rel_path = "rel.py"
        report = run_static_checks([rel_path], str(tmp_path))
        assert report.files_checked == 1

    def test_unknown_import_gives_warn(self, tmp_path):
        p = tmp_path / "user.py"
        p.write_text("import totally_fake_package_qwerty123\n")
        report = run_static_checks([str(p)], str(tmp_path))
        assert not report.has_errors  # WARN not ERROR
        warns = report.warnings
        assert any("totally_fake_package_qwerty123" in f.message for f in warns)

    def test_missing_symbol_gives_error(self, tmp_path):
        # Create workspace module
        mod = tmp_path / "mymod.py"
        mod.write_text("def real_fn(): pass\n")
        # User file imports nonexistent symbol
        user = tmp_path / "user.py"
        user.write_text("from mymod import ghost_fn\n")
        report = run_static_checks([str(user)], str(tmp_path))
        assert report.has_errors
        assert any("ghost_fn" in f.message for f in report.errors)

    def test_structural_regression_detected(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("def foo(): pass\n")
        before = {"a.py": "def foo(): pass\ndef bar(): pass\n"}
        report = run_static_checks([str(p)], str(tmp_path), before_snapshots=before)
        warns = report.warnings
        assert any("bar" in f.message for f in warns)

    def test_structural_regression_none_when_no_snapshots(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("def foo(): pass\n")
        # No before_snapshots → no structural check
        report = run_static_checks([str(p)], str(tmp_path))
        struct_warns = [
            f for f in report.warnings
            if "Top-level name" in f.message
        ]
        assert struct_warns == []

    def test_multiple_files_all_checked(self, tmp_path):
        for name in ["a.py", "b.py", "c.py"]:
            (tmp_path / name).write_text("x = 1\n")
        txt = tmp_path / "readme.txt"
        txt.write_text("ignored")
        paths = [str(tmp_path / n) for n in ["a.py", "b.py", "c.py", "readme.txt"]]
        report = run_static_checks(paths, str(tmp_path))
        assert report.files_checked == 3
        assert report.files_skipped == 1

    def test_syntax_error_skips_import_check(self, tmp_path):
        # When syntax is invalid, import check should not run (no double-findings)
        p = tmp_path / "bad.py"
        p.write_text("def broken(:\n    import os\n")
        report = run_static_checks([str(p)], str(tmp_path))
        errors = report.errors
        assert len(errors) == 1
        assert "syntax" in errors[0].message.lower()

    def test_summary_integrated(self, tmp_path):
        p = tmp_path / "ok.py"
        p.write_text("import sys\n")
        report = run_static_checks([str(p)], str(tmp_path))
        s = report.summary
        assert "1 checked" in s
        assert "0 skipped" in s

    def test_to_prompt_block_empty_when_no_py_files(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("text")
        report = run_static_checks([str(txt)], str(tmp_path))
        assert report.to_prompt_block() == ""


# ---------------------------------------------------------------------------
# _is_stdlib_or_installed
# ---------------------------------------------------------------------------

class TestIsStdlibOrInstalled:
    def test_os_is_stdlib(self):
        assert _is_stdlib_or_installed("os") is True

    def test_sys_is_stdlib(self):
        assert _is_stdlib_or_installed("sys") is True

    def test_pathlib_is_stdlib(self):
        assert _is_stdlib_or_installed("pathlib") is True

    def test_nonexistent_is_false(self):
        assert _is_stdlib_or_installed("definitely_not_real_xyz999") is False

    def test_dotted_module_top_level_checked(self):
        # os.path → top level is "os" which is stdlib
        assert _is_stdlib_or_installed("os.path") is True


# ---------------------------------------------------------------------------
# _module_file_in_workspace
# ---------------------------------------------------------------------------

class TestModuleFileInWorkspace:
    def test_finds_plain_module(self, tmp_path):
        p = tmp_path / "mymod.py"
        p.write_text("")
        result = _module_file_in_workspace("mymod", tmp_path)
        assert result == p

    def test_finds_package_init(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        init = pkg / "__init__.py"
        init.write_text("")
        result = _module_file_in_workspace("mypkg", tmp_path)
        assert result == init

    def test_dotted_module(self, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        mod = sub / "sub.py"
        mod.write_text("")
        result = _module_file_in_workspace("pkg.sub", tmp_path)
        assert result == mod

    def test_returns_none_when_missing(self, tmp_path):
        result = _module_file_in_workspace("ghost", tmp_path)
        assert result is None
