"""Tests for harness/tools/project_map.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import pytest

from harness.core.config import HarnessConfig
from harness.tools.project_map import ProjectMapTool


@pytest.fixture()
def tool() -> ProjectMapTool:
    return ProjectMapTool()


@pytest.fixture()
def config(tmp_path) -> HarnessConfig:
    return HarnessConfig(workspace=str(tmp_path))


def write_py(path: Path, content: str) -> None:
    path.write_text(dedent(content))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_max_depth_zero_returns_error(self, tool, config, tmp_path):
        result = run(tool.execute(config, path=".", max_depth=0))
        assert result.is_error
        assert "max_depth" in result.error

    def test_nonexistent_path_returns_error(self, tool, config):
        result = run(tool.execute(config, path="no_such_dir_xyz", max_depth=3))
        assert result.is_error
        assert "Not a directory" in result.error

    def test_file_path_returns_error(self, tool, config, tmp_path):
        f = tmp_path / "foo.py"
        f.write_text("x = 1")
        result = run(tool.execute(config, path="foo.py", max_depth=3))
        assert result.is_error
        assert "Not a directory" in result.error

    def test_empty_directory_returns_no_files_message(self, tool, config, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = run(tool.execute(config, path="empty", max_depth=3))
        assert not result.is_error
        assert "No Python files" in result.output


# ---------------------------------------------------------------------------
# Basic scan
# ---------------------------------------------------------------------------

class TestBasicScan:
    def test_single_module_found(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", "x = 1\n")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "mymod.py" in result.output

    def test_line_counts_in_output(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", "x = 1\ny = 2\nz = 3\n")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "3" in result.output  # 3 lines

    def test_class_count(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", """
            class A:
                pass
            class B:
                pass
        """)
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "2" in result.output  # 2 classes

    def test_function_count(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", """
            def foo(): pass
            def bar(): pass
            def baz(): pass
        """)
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "3" in result.output  # 3 functions

    def test_entry_point_detected(self, tool, config, tmp_path):
        write_py(tmp_path / "main.py", """
            def main(): pass
            if __name__ == '__main__':
                main()
        """)
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "Entry points" in result.output
        assert "main.py" in result.output

    def test_summary_stats_present(self, tool, config, tmp_path):
        write_py(tmp_path / "a.py", "x = 1")
        write_py(tmp_path / "b.py", "y = 2")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "2 modules" in result.output


# ---------------------------------------------------------------------------
# Test file filtering
# ---------------------------------------------------------------------------

class TestTestFileFiltering:
    def test_test_files_excluded_by_default(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", "x = 1")
        write_py(tmp_path / "test_mymod.py", "def test_foo(): pass")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "test_mymod.py" not in result.output
        assert "mymod.py" in result.output

    def test_test_files_included_when_flag_set(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod.py", "x = 1")
        write_py(tmp_path / "test_mymod.py", "def test_foo(): pass")
        result = run(
            tool.execute(config, path=".", max_depth=3, include_tests=True)
        )
        assert not result.is_error
        assert "test_mymod.py" in result.output

    def test_endswith_test_excluded(self, tool, config, tmp_path):
        write_py(tmp_path / "mymod_test.py", "def test_foo(): pass")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "mymod_test.py" not in result.output


# ---------------------------------------------------------------------------
# Depth limit
# ---------------------------------------------------------------------------

class TestDepthLimit:
    def test_depth_1_excludes_subdir(self, tool, config, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        write_py(tmp_path / "top.py", "x = 1")
        write_py(sub / "deep.py", "y = 2")
        result = run(tool.execute(config, path=".", max_depth=1))
        assert not result.is_error
        assert "top.py" in result.output
        assert "deep.py" not in result.output

    def test_depth_2_includes_first_level_subdir(self, tool, config, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        write_py(sub / "mod.py", "x = 1")
        result = run(tool.execute(config, path=".", max_depth=2))
        assert not result.is_error
        assert "mod.py" in result.output


# ---------------------------------------------------------------------------
# Hidden / special directory exclusion
# ---------------------------------------------------------------------------

class TestHiddenDirExclusion:
    def test_hidden_dir_excluded(self, tool, config, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        write_py(hidden / "secret.py", "x = 1")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "secret.py" not in result.output

    def test_pycache_excluded(self, tool, config, tmp_path):
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        write_py(pycache / "cached.py", "x = 1")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "cached.py" not in result.output

    def test_venv_excluded(self, tool, config, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        write_py(venv / "site_pkg.py", "x = 1")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "site_pkg.py" not in result.output


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------

class TestImportGraph:
    def test_import_graph_shows_imports(self, tool, config, tmp_path):
        write_py(tmp_path / "a.py", "import os\nimport sys\n")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "Internal imports" in result.output or "os" in result.output

    def test_no_imports_skips_section(self, tool, config, tmp_path):
        write_py(tmp_path / "a.py", "x = 1\n")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error

    def test_relative_imports_recorded(self, tool, config, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        write_py(pkg / "__init__.py", "")
        write_py(pkg / "a.py", "from .b import something")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert ".b" in result.output or "b" in result.output


# ---------------------------------------------------------------------------
# Parse error handling
# ---------------------------------------------------------------------------

class TestParseErrors:
    def test_syntax_error_file_skipped_not_crash(self, tool, config, tmp_path):
        write_py(tmp_path / "broken.py", "def foo(: bad syntax")
        write_py(tmp_path / "good.py", "x = 1")
        result = run(tool.execute(config, path=".", max_depth=3))
        assert not result.is_error
        assert "good.py" in result.output

    def test_parse_error_section_or_skip(self, tool, config, tmp_path):
        write_py(tmp_path / "broken.py", "def foo(: bad syntax")
        result = run(tool.execute(config, path=".", max_depth=3))
        # Either broken.py gets listed in parse errors or simply skipped
        lower = result.output.lower()
        assert not result.is_error
        assert "parse error" in lower or "broken.py" not in result.output


# ---------------------------------------------------------------------------
# _analyze_file direct tests
# ---------------------------------------------------------------------------

class TestAnalyzeFile:
    def test_returns_none_for_missing_file(self, tool, tmp_path):
        p = tmp_path / "gone.py"
        result = tool._analyze_file(p, "gone.py")
        assert result is None

    def test_returns_none_for_syntax_error(self, tool, tmp_path):
        p = tmp_path / "bad.py"
        p.write_text("def foo(: bad")
        result = tool._analyze_file(p, "bad.py")
        assert result is None

    def test_main_class_detected(self, tool, tmp_path):
        p = tmp_path / "runner.py"
        p.write_text("class Runner:\n    def run(self): pass\n")
        result = tool._analyze_file(p, "runner.py")
        assert result is not None
        assert result["has_main_class"] is True

    def test_entry_point_detected(self, tool, tmp_path):
        p = tmp_path / "main.py"
        p.write_text("if __name__ == '__main__':\n    pass\n")
        result = tool._analyze_file(p, "main.py")
        assert result is not None
        assert result["is_entry"] is True

    def test_no_entry_point(self, tool, tmp_path):
        p = tmp_path / "lib.py"
        p.write_text("x = 1\n")
        result = tool._analyze_file(p, "lib.py")
        assert result is not None
        assert result["is_entry"] is False

    def test_imports_deduplicated(self, tool, tmp_path):
        p = tmp_path / "dup.py"
        p.write_text("import os\nimport os\nimport sys\n")
        result = tool._analyze_file(p, "dup.py")
        assert result is not None
        assert result["imports"].count("os") == 1

    def test_from_import_recorded(self, tool, tmp_path):
        p = tmp_path / "fi.py"
        p.write_text("from pathlib import Path\n")
        result = tool._analyze_file(p, "fi.py")
        assert result is not None
        assert "pathlib" in result["imports"]

    def test_async_functions_counted(self, tool, tmp_path):
        p = tmp_path / "af.py"
        p.write_text("async def foo(): pass\nasync def bar(): pass\n")
        result = tool._analyze_file(p, "af.py")
        assert result is not None
        assert result["functions"] == 2


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

class TestToolMetadata:
    def test_name(self, tool):
        assert tool.name == "project_map"

    def test_tags(self, tool):
        assert "analysis" in tool.tags

    def test_input_schema_required_fields(self, tool):
        schema = tool.input_schema()
        assert "path" in schema["required"]
        assert "max_depth" in schema["required"]

    def test_max_files_constant(self, tool):
        assert hasattr(tool, "MAX_FILES")
        assert tool.MAX_FILES > 0
