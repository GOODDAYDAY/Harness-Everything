"""Tests for harness/tools/search_grep.py (GrepSearchTool)."""
from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.search_grep import GrepSearchTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: pathlib.Path):
    cfg = MagicMock()
    cfg.workspace = str(tmp_path)
    cfg.allowed_paths = [tmp_path]
    return cfg


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tool():
    return GrepSearchTool()


@pytest.fixture()
def py_file(tmp_path):
    p = tmp_path / "code.py"
    p.write_text("def hello():\n    print('hello world')\n# comment\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------

class TestBasicMatching:
    def test_finds_matches(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern="hello", limit=10, context_lines=0))
        assert not r.is_error
        assert "code.py" in r.output
        assert "Found 2 match" in r.output

    def test_match_includes_line_number(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern="hello", limit=10, context_lines=0))
        assert "code.py:1" in r.output
        assert "code.py:2" in r.output

    def test_no_match_reports_none(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern="ZZZNOMATCH", limit=10, context_lines=0))
        assert not r.is_error
        assert "No matches" in r.output

    def test_regex_pattern(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern=r"def \w+", limit=10, context_lines=0))
        assert "code.py" in r.output
        assert "hello" in r.output


# ---------------------------------------------------------------------------
# Case sensitivity
# ---------------------------------------------------------------------------

class TestCaseSensitivity:
    def test_case_sensitive_default_no_match(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern="HELLO", limit=10, context_lines=0))
        assert "No matches" in r.output

    def test_case_insensitive_finds_match(self, tool, py_file):
        r = _run(tool.execute(_cfg(py_file), pattern="HELLO", limit=10,
                               context_lines=0, case_insensitive=True))
        assert "code.py" in r.output
        assert "Found" in r.output


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------

class TestContextLines:
    def test_context_lines_shows_neighbours(self, tool, tmp_path):
        p = tmp_path / "ctx.py"
        p.write_text("line1\nline2\nTARGET\nline4\nline5\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="TARGET", limit=10, context_lines=1))
        assert "line2" in r.output
        assert "line4" in r.output

    def test_zero_context_no_neighbours(self, tool, tmp_path):
        p = tmp_path / "ctx.py"
        p.write_text("line1\nTARGET\nline3\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="TARGET", limit=10, context_lines=0))
        assert "line1" not in r.output
        assert "line3" not in r.output
        assert "TARGET" in r.output


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_caps_results(self, tool, tmp_path):
        # create a file with 5 matches
        p = tmp_path / "many.py"
        p.write_text("\n".join(f"foo line{i}" for i in range(5)) + "\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="foo", limit=2, context_lines=0))
        # 'showing first N' or 'Found 2'
        assert "Found 2" in r.output or "showing first" in r.output.lower()
        # Count matching data lines (exclude header line)
        match_lines = [line for line in r.output.splitlines() if "many.py:" in line]
        assert len(match_lines) <= 2


# ---------------------------------------------------------------------------
# File glob filter
# ---------------------------------------------------------------------------

class TestFileGlob:
    def test_file_glob_py_only(self, tool, tmp_path):
        (tmp_path / "code.py").write_text("alpha\n")
        (tmp_path / "data.txt").write_text("alpha\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="alpha", limit=10,
                               context_lines=0, file_glob="*.py"))
        assert "code.py" in r.output
        assert "data.txt" not in r.output

    def test_file_glob_txt_only(self, tool, tmp_path):
        (tmp_path / "code.py").write_text("alpha\n")
        (tmp_path / "data.txt").write_text("alpha\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="alpha", limit=10,
                               context_lines=0, file_glob="*.txt"))
        assert "data.txt" in r.output
        assert "code.py" not in r.output


# ---------------------------------------------------------------------------
# Sub-directory path filter
# ---------------------------------------------------------------------------

class TestPathFilter:
    def test_path_restricts_to_subdir(self, tool, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.py").write_text("needle\n")
        (sub / "nested.py").write_text("needle\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="needle", limit=10,
                               context_lines=0, path=str(sub)))
        assert "nested.py" in r.output
        assert "root.py" not in r.output


# ---------------------------------------------------------------------------
# Multiple files
# ---------------------------------------------------------------------------

class TestMultipleFiles:
    def test_matches_across_multiple_files(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.py").write_text("needle\n")
        r = _run(tool.execute(_cfg(tmp_path), pattern="needle", limit=10, context_lines=0))
        assert "a.py" in r.output
        assert "b.py" in r.output
