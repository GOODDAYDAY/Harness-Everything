"""Tests for harness/tools/call_graph.py (CallGraphTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.call_graph import CallGraphTool


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
    return CallGraphTool()


@pytest.fixture()
def simple_module(tmp_path):
    """foo -> bar -> baz"""
    p = tmp_path / "mod.py"
    p.write_text(
        "def foo():\n"
        "    return bar()\n\n"
        "def bar():\n"
        "    return baz()\n\n"
        "def baz():\n"
        "    return 42\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Basic graph construction
# ---------------------------------------------------------------------------

class TestBasicGraph:
    def test_finds_direct_callee(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=1, function_name="foo", root=str(simple_module)
        ))
        assert not r.is_error
        data = json.loads(r.output)
        assert "bar" in data["graph"]["foo"]["calls"]

    def test_depth_limits_traversal(self, tool, simple_module):
        r1 = _run(tool.execute(
            _cfg(simple_module), depth=1, function_name="foo", root=str(simple_module)
        ))
        r2 = _run(tool.execute(
            _cfg(simple_module), depth=2, function_name="foo", root=str(simple_module)
        ))
        data1 = json.loads(r1.output)
        data2 = json.loads(r2.output)
        # depth=1 includes foo + bar; depth=2 should include foo, bar, baz
        assert data1["nodes_total"] < data2["nodes_total"]

    def test_root_function_in_graph(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=2, function_name="foo", root=str(simple_module)
        ))
        data = json.loads(r.output)
        assert "foo" in data["graph"]
        assert data["root_function"] == "foo"

    def test_leaf_node_has_empty_calls(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=3, function_name="foo", root=str(simple_module)
        ))
        data = json.loads(r.output)
        assert data["graph"]["baz"]["calls"] == []

    def test_found_nodes_have_file_and_line(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=1, function_name="foo", root=str(simple_module)
        ))
        data = json.loads(r.output)
        node = data["graph"]["foo"]
        assert node["file"] is not None
        assert node["line"] is not None
        assert node["found"] is True


# ---------------------------------------------------------------------------
# Unknown / not-found function
# ---------------------------------------------------------------------------

class TestUnknownFunction:
    def test_unknown_function_not_error(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=1, function_name="nonexistent", root=str(simple_module)
        ))
        assert not r.is_error

    def test_unknown_function_found_is_false(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=1, function_name="nonexistent", root=str(simple_module)
        ))
        data = json.loads(r.output)
        assert data["graph"]["nonexistent"]["found"] is False
        assert data["nodes_found"] == 0


# ---------------------------------------------------------------------------
# include_builtins
# ---------------------------------------------------------------------------

class TestIncludeBuiltins:
    def test_include_builtins_shows_len(self, tool, tmp_path):
        (tmp_path / "m.py").write_text(
            "def greet(name):\n"
            "    return len(name) > 0\n"
        )
        r = _run(tool.execute(
            _cfg(tmp_path), depth=1, function_name="greet",
            root=str(tmp_path), include_builtins=True
        ))
        data = json.loads(r.output)
        assert "len" in data["graph"]

    def test_exclude_builtins_omits_len(self, tool, tmp_path):
        (tmp_path / "m.py").write_text(
            "def greet(name):\n"
            "    return len(name) > 0\n"
        )
        r = _run(tool.execute(
            _cfg(tmp_path), depth=1, function_name="greet",
            root=str(tmp_path), include_builtins=False
        ))
        data = json.loads(r.output)
        assert "len" not in data["graph"]


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def test_output_is_valid_json(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=2, function_name="foo", root=str(simple_module)
        ))
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, dict)

    def test_output_has_required_keys(self, tool, simple_module):
        r = _run(tool.execute(
            _cfg(simple_module), depth=2, function_name="foo", root=str(simple_module)
        ))
        data = json.loads(r.output)
        for key in ["root_function", "depth", "nodes_total",
                    "nodes_found", "nodes_external", "truncated", "graph"]:
            assert key in data, f"missing key: {key}"

    def test_depth_field_matches_input(self, tool, simple_module):
        for depth in [1, 2, 3]:
            r = _run(tool.execute(
                _cfg(simple_module), depth=depth, function_name="foo", root=str(simple_module)
            ))
            data = json.loads(r.output)
            assert data["depth"] == depth
