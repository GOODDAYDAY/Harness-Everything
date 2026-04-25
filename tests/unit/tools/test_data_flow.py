"""Tests for harness/tools/data_flow.py (DataFlowTool)."""
from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.data_flow import DataFlowTool


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
    return DataFlowTool()


@pytest.fixture()
def caller_module(tmp_path):
    """helper() is called from caller_a() and caller_b()."""
    (tmp_path / "a.py").write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def caller_a():\n"
        "    return helper()\n\n"
        "def caller_b():\n"
        "    return helper()\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# callers mode
# ---------------------------------------------------------------------------

class TestCallersMode:
    def test_finds_direct_callers(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="helper", depth=1, mode="callers",
            root=str(caller_module)
        ))
        assert not r.is_error
        data = json.loads(r.output)
        enclosing = [item["enclosing_function"] for item in data["results"]]
        assert "caller_a" in enclosing
        assert "caller_b" in enclosing

    def test_no_callers_returns_empty_list(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="orphan_func", depth=1, mode="callers",
            root=str(caller_module)
        ))
        data = json.loads(r.output)
        assert data["results"] == []

    def test_result_includes_file_and_line(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="helper", depth=1, mode="callers",
            root=str(caller_module)
        ))
        data = json.loads(r.output)
        assert len(data["results"]) > 0
        for item in data["results"]:
            assert "file" in item
            assert "line" in item
            assert "enclosing_function" in item

    def test_output_is_valid_json(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="helper", depth=1, mode="callers",
            root=str(caller_module)
        ))
        data = json.loads(r.output)  # must not raise
        assert isinstance(data, dict)

    def test_output_has_symbol_and_mode(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="helper", depth=1, mode="callers",
            root=str(caller_module)
        ))
        data = json.loads(r.output)
        assert data["symbol"] == "helper"
        assert data["mode"] == "callers"


# ---------------------------------------------------------------------------
# call_chain mode
# ---------------------------------------------------------------------------

class TestCallChainMode:
    def test_call_chain_l1_callers(self, tool, caller_module):
        r = _run(tool.execute(
            _cfg(caller_module), symbol="helper", depth=2, mode="call_chain",
            root=str(caller_module)
        ))
        assert not r.is_error
        data = json.loads(r.output)
        results = data["results"]
        l1 = [item["enclosing_function"] for item in results["l1_callers"]]
        assert "caller_a" in l1
        assert "caller_b" in l1

    def test_call_chain_l2_callers_present(self, tool, tmp_path):
        """A -> B -> C: call_chain on C shows B as l1, A as l2."""
        (tmp_path / "m.py").write_text(
            "def c():\n"
            "    return 1\n\n"
            "def b():\n"
            "    return c()\n\n"
            "def a():\n"
            "    return b()\n"
        )
        tool2 = DataFlowTool()
        r = _run(tool2.execute(
            _cfg(tmp_path), symbol="c", depth=2, mode="call_chain",
            root=str(tmp_path)
        ))
        data = json.loads(r.output)
        results = data["results"]
        l1 = [item["enclosing_function"] for item in results["l1_callers"]]
        assert "b" in l1
        # l2_callers: for each l1 caller, who calls them?
        assert "b" in results["l2_callers"]
        l2_of_b = [item["enclosing_function"] for item in results["l2_callers"]["b"]]
        assert "a" in l2_of_b

    def test_call_chain_no_callers(self, tool, tmp_path):
        (tmp_path / "m.py").write_text("def standalone():\n    return 42\n")
        r = _run(tool.execute(
            _cfg(tmp_path), symbol="standalone", depth=2, mode="call_chain",
            root=str(tmp_path)
        ))
        data = json.loads(r.output)
        assert data["results"]["l1_callers"] == []


# ---------------------------------------------------------------------------
# reads mode
# ---------------------------------------------------------------------------

class TestReadsMode:
    def test_reads_mode_returns_results_key(self, tool, tmp_path):
        (tmp_path / "a.py").write_text(
            "class Config:\n"
            "    timeout = 30\n"
        )
        r = _run(tool.execute(
            _cfg(tmp_path), symbol="Config.timeout", depth=1, mode="reads",
            root=str(tmp_path)
        ))
        assert not r.is_error
        data = json.loads(r.output)
        assert "results" in data
        assert data["mode"] == "reads"
        assert data["symbol"] == "Config.timeout"

    def test_reads_mode_no_results_empty(self, tool, tmp_path):
        (tmp_path / "a.py").write_text("def unused(): pass\n")
        r = _run(tool.execute(
            _cfg(tmp_path), symbol="Foo.bar", depth=1, mode="reads",
            root=str(tmp_path)
        ))
        data = json.loads(r.output)
        assert isinstance(data["results"], list)


# ---------------------------------------------------------------------------
# Root scoping
# ---------------------------------------------------------------------------

class TestRootScoping:
    def test_root_restricts_search(self, tool, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "outer.py").write_text(
            "def target(): pass\n\ndef calls_target():\n    return target()\n"
        )
        (sub / "inner.py").write_text(
            "def calls_target():\n    return target()\n"
        )
        r_sub = _run(tool.execute(
            _cfg(tmp_path), symbol="target", depth=1, mode="callers",
            root=str(sub)
        ))
        r_all = _run(tool.execute(
            _cfg(tmp_path), symbol="target", depth=1, mode="callers",
            root=str(tmp_path)
        ))
        data_sub = json.loads(r_sub.output)
        data_all = json.loads(r_all.output)
        # sub-scoped search should find fewer (or equal) results
        assert len(data_sub["results"]) <= len(data_all["results"])
