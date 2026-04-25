"""Unit tests for harness.evaluation.static_analysis.

Tests cover all four checks:
  1. Syntax validity (_check_syntax)
  2. Import sanity (_check_imports — unknown top-level module)
  3. Symbol existence (_check_imports — from X import Y)
  4. Structural regression (_check_structural_regression)

Plus the public entry point (run_static_checks) and StaticReport utilities.
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
# Helpers
# ---------------------------------------------------------------------------

def dedent(s: str) -> str:
    return textwrap.dedent(s).lstrip()


# ---------------------------------------------------------------------------
# Finding + StaticReport
# ---------------------------------------------------------------------------

class TestFinding:
    def test_basic_fields(self):
        f = Finding(level="ERROR", file="foo.py", message="bad", line=10)
        assert f.level == "ERROR"
        assert f.file == "foo.py"
        assert f.message == "bad"
        assert f.line == 10

    def test_default_line_zero(self):
        f = Finding(level="WARN", file="bar.py", message="meh")
        assert f.line == 0


class TestStaticReport:
    def _make_report(self) -> StaticReport:
        r = StaticReport()
        r.files_checked = 3
        r.files_skipped = 1
        r.findings = [
            Finding(level="ERROR", file="a.py", message="syntax error", line=5),
            Finding(level="WARN", file="b.py", message="unknown import", line=2),
            Finding(level="WARN", file="a.py", message="another warn", line=8),
        ]
        return r

    def test_errors_property(self):
        r = self._make_report()
        assert len(r.errors) == 1
        assert r.errors[0].level == "ERROR"

    def test_warnings_property(self):
        r = self._make_report()
        assert len(r.warnings) == 2

    def test_has_errors_true(self):
        r = self._make_report()
        assert r.has_errors is True

    def test_has_errors_false(self):
        r = StaticReport()
        r.files_checked = 2
        r.findings = [Finding(level="WARN", file="x.py", message="w")]
        assert r.has_errors is False

    def test_summary_format(self):
        r = self._make_report()
        s = r.summary
        assert "1 error" in s
        assert "2 warning" in s
        assert "checked" in s

    def test_to_prompt_block_empty(self):
        r = StaticReport()  # files_checked == 0
        assert r.to_prompt_block() == ""

    def test_to_prompt_block_clean(self):
        r = StaticReport()
        r.files_checked = 2
        block = r.to_prompt_block()
        assert "Static Analysis Results" in block
        assert "passed" in block
        assert "✓" in block

    def test_to_prompt_block_with_findings(self):
        r = self._make_report()
        block = r.to_prompt_block()
        assert "ERROR" in block
        assert "WARN" in block
        # Table format
        assert "|" in block
        # Error notice
        assert "⚠" in block

    def test_to_prompt_block_escapes_pipes_in_message(self):
        r = StaticReport()
        r.files_checked = 1
        r.findings = [Finding(level="WARN", file="f.py", message="a|b", line=1)]
        block = r.to_prompt_block()
        assert "a\\|b" in block


# ---------------------------------------------------------------------------
# _get_top_level_names
# ---------------------------------------------------------------------------

class TestGetTopLevelNames:
    def test_empty_source(self):
        assert _get_top_level_names("") == set()

    def test_functions_and_classes(self):
        src = dedent("""\
            def foo(): pass
            async def bar(): pass
            class Baz: pass
        """)
        names = _get_top_level_names(src)
        assert names == {"foo", "bar", "Baz"}

    def test_nested_not_included(self):
        src = dedent("""\
            def outer():
                def inner(): pass
                class Hidden: pass
        """)
        names = _get_top_level_names(src)
        assert "outer" in names
        assert "inner" not in names
        assert "Hidden" not in names

    def test_syntax_error_returns_empty(self):
        names = _get_top_level_names("def (") 
        assert names == set()


# ---------------------------------------------------------------------------
# _module_file_in_workspace
# ---------------------------------------------------------------------------

class TestModuleFileInWorkspace:
    def test_plain_module_found(self, tmp_path):
        (tmp_path / "mymod.py").write_text("x = 1")
        result = _module_file_in_workspace("mymod", tmp_path)
        assert result == tmp_path / "mymod.py"

    def test_dotted_module_found(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "sub.py").write_text("x = 1")
        result = _module_file_in_workspace("pkg.sub", tmp_path)
        assert result == pkg / "sub.py"

    def test_package_init_found(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        result = _module_file_in_workspace("pkg", tmp_path)
        assert result == pkg / "__init__.py"

    def test_not_found_returns_none(self, tmp_path):
        result = _module_file_in_workspace("nonexistent", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _is_stdlib_or_installed
# ---------------------------------------------------------------------------

class TestIsStdlibOrInstalled:
    def test_stdlib_os(self):
        assert _is_stdlib_or_installed("os") is True

    def test_stdlib_sys(self):
        assert _is_stdlib_or_installed("sys") is True

    def test_stdlib_json(self):
        assert _is_stdlib_or_installed("json") is True

    def test_dotted_stdlib(self):
        assert _is_stdlib_or_installed("os.path") is True

    def test_fake_module_false(self):
        assert _is_stdlib_or_installed("definitely_not_a_real_module_xyz") is False

    def test_installed_pytest(self):
        # pytest is installed in our test env
        assert _is_stdlib_or_installed("pytest") is True


# ---------------------------------------------------------------------------
# _check_syntax
# ---------------------------------------------------------------------------

class TestCheckSyntax:
    def test_valid_file_no_findings(self, tmp_path):
        p = tmp_path / "good.py"
        p.write_text("x = 1\ndef f(): return x\n")
        findings = _check_syntax(p, "good.py")
        assert findings == []

    def test_syntax_error_returns_error_finding(self, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def (: pass\n")
        findings = _check_syntax(p, "bad.py")
        assert len(findings) == 1
        f = findings[0]
        assert f.level == "ERROR"
        assert f.file == "bad.py"
        assert "syntax" in f.message.lower()

    def test_syntax_error_extracts_line_number(self, tmp_path):
        p = tmp_path / "bad2.py"
        # Syntax error on line 3
        p.write_text("x = 1\ny = 2\ndef (: pass\n")
        findings = _check_syntax(p, "bad2.py")
        assert findings[0].line > 0


# ---------------------------------------------------------------------------
# _check_imports
# ---------------------------------------------------------------------------

class TestCheckImports:
    def test_stdlib_import_ok(self, tmp_path):
        src = "import os\nimport sys\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []

    def test_unknown_top_level_import_warns(self, tmp_path):
        src = "import totally_fake_module_xyz\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert len(findings) == 1
        assert findings[0].level == "WARN"
        assert "totally_fake_module_xyz" in findings[0].message

    def test_unknown_from_import_warns(self, tmp_path):
        src = "from totally_fake_module_xyz import something\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert len(findings) == 1
        assert findings[0].level == "WARN"

    def test_valid_from_import_in_workspace(self, tmp_path):
        # Create a module in workspace that exports 'my_func'
        mod = tmp_path / "mymod.py"
        mod.write_text("def my_func(): pass\nMY_CONST = 1\n")
        src = "from mymod import my_func\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []

    def test_missing_symbol_from_workspace_module_errors(self, tmp_path):
        mod = tmp_path / "mymod.py"
        mod.write_text("def existing(): pass\n")
        src = "from mymod import nonexistent_symbol\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert len(findings) == 1
        assert findings[0].level == "ERROR"
        assert "nonexistent_symbol" in findings[0].message

    def test_star_import_skipped(self, tmp_path):
        mod = tmp_path / "mymod.py"
        mod.write_text("def f(): pass\n")
        src = "from mymod import *\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []

    def test_syntax_error_in_source_returns_empty(self, tmp_path):
        src = "def ("
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []

    def test_package_submodule_import_valid(self, tmp_path):
        # from pkg import submod should be valid when pkg/__init__.py and pkg/submod.py exist
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "submod.py").write_text("X = 1\n")
        src = "from pkg import submod\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []

    def test_constant_in_module_importable(self, tmp_path):
        mod = tmp_path / "constants.py"
        mod.write_text("MY_CONST = 42\nOTHER: int = 7\n")
        src = "from constants import MY_CONST, OTHER\n"
        findings = _check_imports(src, "test.py", tmp_path)
        assert findings == []


# ---------------------------------------------------------------------------
# _check_structural_regression
# ---------------------------------------------------------------------------

class TestCheckStructuralRegression:
    def test_no_before_source_returns_empty(self, tmp_path):
        p = tmp_path / "new.py"
        p.write_text("def f(): pass\n")
        findings = _check_structural_regression(p, "new.py", before_source=None)
        assert findings == []

    def test_no_regression_returns_empty(self, tmp_path):
        before = "def foo(): pass\ndef bar(): pass\n"
        p = tmp_path / "same.py"
        p.write_text(before)
        findings = _check_structural_regression(p, "same.py", before_source=before)
        assert findings == []

    def test_removed_name_warns(self, tmp_path):
        before = "def foo(): pass\ndef bar(): pass\n"
        after = "def foo(): pass\n"  # bar removed
        p = tmp_path / "changed.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "changed.py", before_source=before)
        assert len(findings) == 1
        assert findings[0].level == "WARN"
        assert "bar" in findings[0].message

    def test_added_name_no_warning(self, tmp_path):
        before = "def foo(): pass\n"
        after = "def foo(): pass\ndef new_func(): pass\n"
        p = tmp_path / "expanded.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "expanded.py", before_source=before)
        assert findings == []

    def test_multiple_removed_names_all_warned(self, tmp_path):
        before = "class A: pass\nclass B: pass\ndef c(): pass\n"
        after = "class A: pass\n"
        p = tmp_path / "reduced.py"
        p.write_text(after)
        findings = _check_structural_regression(p, "reduced.py", before_source=before)
        assert len(findings) == 2
        removed_names = {f.message.split("'")[1] for f in findings}
        assert removed_names == {"B", "c"}


# ---------------------------------------------------------------------------
# run_static_checks — end-to-end
# ---------------------------------------------------------------------------

class TestRunStaticChecks:
    def test_empty_file_list(self, tmp_path):
        report = run_static_checks([], workspace=str(tmp_path))
        assert report.files_checked == 0
        assert report.files_skipped == 0
        assert report.findings == []

    def test_non_python_file_skipped(self, tmp_path):
        txt = tmp_path / "readme.txt"
        txt.write_text("hello")
        report = run_static_checks([str(txt)], workspace=str(tmp_path))
        assert report.files_skipped == 1
        assert report.files_checked == 0

    def test_missing_file_skipped(self, tmp_path):
        report = run_static_checks(
            [str(tmp_path / "ghost.py")], workspace=str(tmp_path)
        )
        assert report.files_skipped == 1
        assert report.files_checked == 0

    def test_clean_file_no_findings(self, tmp_path):
        p = tmp_path / "clean.py"
        p.write_text("import os\n\ndef greet(name: str) -> str:\n    return f'Hello {name}'\n")
        report = run_static_checks([str(p)], workspace=str(tmp_path))
        assert report.files_checked == 1
        assert report.files_skipped == 0
        assert report.findings == []
        assert report.has_errors is False

    def test_syntax_error_reported_as_error(self, tmp_path):
        p = tmp_path / "broken.py"
        p.write_text("def broken(:\n    pass\n")
        report = run_static_checks([str(p)], workspace=str(tmp_path))
        assert report.has_errors is True
        assert len(report.errors) >= 1

    def test_relative_path_resolved(self, tmp_path):
        p = tmp_path / "rel.py"
        p.write_text("x = 1\n")
        # Pass relative path instead of absolute
        report = run_static_checks(["rel.py"], workspace=str(tmp_path))
        assert report.files_checked == 1

    def test_before_snapshots_detects_regression(self, tmp_path):
        p = tmp_path / "mod.py"
        before = "def keep(): pass\ndef removed(): pass\n"
        after = "def keep(): pass\n"
        p.write_text(after)
        report = run_static_checks(
            [str(p)],
            workspace=str(tmp_path),
            before_snapshots={"mod.py": before},
        )
        assert any("removed" in f.message for f in report.findings)

    def test_multiple_files(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text("x = 1\n")
        bad = tmp_path / "bad.py"
        bad.write_text("def (:\n    pass\n")
        report = run_static_checks(
            [str(good), str(bad)], workspace=str(tmp_path)
        )
        assert report.files_checked == 2
        assert report.has_errors is True
        # Only the bad file should have a finding
        error_files = {f.file for f in report.errors}
        assert "bad.py" in error_files
        assert "good.py" not in error_files
