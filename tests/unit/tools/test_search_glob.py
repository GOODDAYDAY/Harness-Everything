"""Tests for harness/tools/search_glob.py (GlobSearchTool)."""
from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.search_glob import GlobSearchTool


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
    return GlobSearchTool()


# ---------------------------------------------------------------------------
# Basic glob matching
# ---------------------------------------------------------------------------

class TestBasicGlob:
    def test_finds_py_files(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.py", limit=20))
        assert not r.is_error
        assert "a.py" in r.output
        assert "b.py" in r.output

    def test_no_match_reports_none(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.xyz_no_ext", limit=20))
        assert not r.is_error
        assert "No files" in r.output

    def test_recursive_glob(self, tool, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.py").write_text("x")
        (sub / "nested.py").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="**/*.py", limit=20))
        assert "root.py" in r.output
        assert "nested.py" in r.output

    def test_extension_filter(self, tool, tmp_path):
        (tmp_path / "code.py").write_text("x")
        (tmp_path / "readme.md").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.md", limit=20))
        assert "readme.md" in r.output
        assert "code.py" not in r.output

    def test_reports_found_count(self, tool, tmp_path):
        for name in ["a.py", "b.py", "c.py"]:
            (tmp_path / name).write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.py", limit=20))
        assert "Found 3" in r.output


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_caps_results(self, tool, tmp_path):
        for name in ["a.py", "b.py", "c.py", "d.py"]:
            (tmp_path / name).write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.py", limit=2))
        assert not r.is_error
        # Should mention truncation or only show 2
        assert "2" in r.output
        # Total matches in output line count
        lines = [line for line in r.output.splitlines() if line.endswith(".py")]
        assert len(lines) <= 2

    def test_limit_of_one(self, tool, tmp_path):
        for name in ["a.py", "b.py"]:
            (tmp_path / name).write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.py", limit=1))
        lines = [line for line in r.output.splitlines() if line.endswith(".py")]
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------

class TestPathScoping:
    def test_path_restricts_search(self, tool, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.py").write_text("x")
        (sub / "deep.py").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="**/*.py", limit=20, path=str(sub)))
        assert "deep.py" in r.output
        assert "root.py" not in r.output


# ---------------------------------------------------------------------------
# Sorted by modification time (output has some ordering)
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_returns_paths(self, tool, tmp_path):
        (tmp_path / "alpha.py").write_text("x")
        r = _run(tool.execute(_cfg(tmp_path), pattern="*.py", limit=10))
        assert "alpha.py" in r.output

    def test_multiple_extensions(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        (tmp_path / "c.md").write_text("x")
        r_all = _run(tool.execute(_cfg(tmp_path), pattern="*.*", limit=20))
        assert "a.py" in r_all.output
        assert "b.txt" in r_all.output
        assert "c.md" in r_all.output
