"""Tests for harness/tools/dependency_analyzer.py (DependencyAnalyzerTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.dependency_analyzer import DependencyAnalyzerTool


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
    return DependencyAnalyzerTool()


@pytest.fixture()
def simple_tree(tmp_path):
    """a -> b -> c (no cycles)"""
    (tmp_path / "a.py").write_text("from b import foo\n")
    (tmp_path / "b.py").write_text("from c import bar\n")
    (tmp_path / "c.py").write_text("def bar(): pass\ndef foo(): pass\n")
    return tmp_path


@pytest.fixture()
def cycle_tree(tmp_path):
    """a -> b -> a (circular)"""
    (tmp_path / "a.py").write_text("from b import foo\n")
    (tmp_path / "b.py").write_text("from a import bar\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Graph mode
# ---------------------------------------------------------------------------

class TestGraphMode:
    def test_returns_json(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph", root=str(simple_tree)))
        assert not r.is_error
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, dict)

    def test_graph_keys_present(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph", root=str(simple_tree)))
        data = json.loads(r.output)
        for key in ["root", "mode", "modules_total", "files_scanned",
                    "cycles_found", "cycles", "graph"]:
            assert key in data, f"missing key: {key}"

    def test_graph_contains_modules(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph", root=str(simple_tree)))
        data = json.loads(r.output)
        assert "a" in data["graph"]
        assert "b" in data["graph"]

    def test_graph_edges_correct(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph", root=str(simple_tree)))
        data = json.loads(r.output)
        assert "b" in data["graph"]["a"]
        assert "c" in data["graph"]["b"]

    def test_no_cycles_in_acyclic_tree(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph", root=str(simple_tree)))
        data = json.loads(r.output)
        assert data["cycles_found"] == 0
        assert data["cycles"] == []


# ---------------------------------------------------------------------------
# Cycles mode
# ---------------------------------------------------------------------------

class TestCyclesMode:
    def test_detects_cycle(self, tool, cycle_tree):
        r = _run(tool.execute(_cfg(cycle_tree), mode="cycles", root=str(cycle_tree)))
        assert not r.is_error
        data = json.loads(r.output)
        assert data["cycles_found"] >= 1

    def test_cycle_contains_both_modules(self, tool, cycle_tree):
        r = _run(tool.execute(_cfg(cycle_tree), mode="cycles", root=str(cycle_tree)))
        data = json.loads(r.output)
        assert len(data["cycles"]) >= 1
        cycle = data["cycles"][0]
        assert "a" in cycle
        assert "b" in cycle

    def test_no_cycle_in_acyclic_tree(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="cycles", root=str(simple_tree)))
        data = json.loads(r.output)
        assert data["cycles_found"] == 0


# ---------------------------------------------------------------------------
# Imports mode
# ---------------------------------------------------------------------------

class TestImportsMode:
    def test_imports_mode_returns_per_file(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="imports", root=str(simple_tree)))
        assert not r.is_error
        data = json.loads(r.output)
        # Should have a 'files' or 'imports' key or per-module breakdown
        assert isinstance(data, dict)

    def test_imports_mode_shows_a_imports_b(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="imports", root=str(simple_tree)))
        # The import list for 'a' should reference 'b'
        assert "b" in r.output  # a imports from b


# ---------------------------------------------------------------------------
# module_filter
# ---------------------------------------------------------------------------

class TestModuleFilter:
    def test_filter_restricts_output(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph",
                               root=str(simple_tree), module_filter="a"))
        data = json.loads(r.output)
        # Only module 'a' should appear in graph
        assert "a" in data["graph"]
        assert "b" not in data["graph"]
        assert "c" not in data["graph"]

    def test_filter_shows_deps_of_matching_module(self, tool, simple_tree):
        r = _run(tool.execute(_cfg(simple_tree), mode="graph",
                               root=str(simple_tree), module_filter="a"))
        data = json.loads(r.output)
        # 'a' depends on 'b'
        assert "b" in data["graph"]["a"]


# ---------------------------------------------------------------------------
# include_stdlib
# ---------------------------------------------------------------------------

class TestIncludeStdlib:
    def test_stdlib_excluded_by_default(self, tool, tmp_path):
        (tmp_path / "m.py").write_text("import os\nimport sys\n")
        r = _run(tool.execute(_cfg(tmp_path), mode="graph", root=str(tmp_path)))
        data = json.loads(r.output)
        # os and sys should not appear as project modules
        assert "os" not in data.get("graph", {})
        assert "sys" not in data.get("graph", {})

    def test_stdlib_included_when_requested(self, tool, tmp_path):
        (tmp_path / "m.py").write_text("import os\n")
        r = _run(tool.execute(_cfg(tmp_path), mode="graph",
                               root=str(tmp_path), include_stdlib=True))
        data = json.loads(r.output)
        # With include_stdlib=True, 'os' should appear in m's deps
        m_deps = data.get("graph", {}).get("m", [])
        assert "os" in m_deps
