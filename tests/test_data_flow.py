"""Tests for the data_flow tool."""

from __future__ import annotations

import asyncio
import json

from harness.core.config import HarnessConfig
from harness.tools.data_flow import DataFlowTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(tool, config, **kwargs):
    """Synchronously execute the tool."""
    return asyncio.run(tool.execute(config, **kwargs))


def _make_config(tmp_path):
    ws = str(tmp_path)
    return HarnessConfig(workspace=ws, allowed_paths=[ws])


# ---------------------------------------------------------------------------
# Initialization / Schema
# ---------------------------------------------------------------------------

class TestDataFlowInit:
    def test_tool_name(self):
        assert DataFlowTool().name == "data_flow"

    def test_description_mentions_modes(self):
        desc = DataFlowTool().description.lower()
        assert "callers" in desc
        assert "reads" in desc
        assert "call_chain" in desc

    def test_schema_required_fields(self):
        schema = DataFlowTool().input_schema()
        required = schema["required"]
        assert "symbol" in required
        assert "depth" in required

    def test_schema_mode_has_enum(self):
        schema = DataFlowTool().input_schema()
        mode_prop = schema["properties"]["mode"]
        assert "enum" in mode_prop
        assert "reads" in mode_prop["enum"]
        assert "callers" in mode_prop["enum"]
        assert "call_chain" in mode_prop["enum"]

    def test_schema_has_root_property(self):
        schema = DataFlowTool().input_schema()
        assert "root" in schema["properties"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestDataFlowValidation:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_empty_symbol_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="", depth=1)
        assert result.is_error
        assert "symbol" in result.error.lower()

    def test_whitespace_only_symbol_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="   ", depth=1)
        assert result.is_error

    def test_unknown_mode_rejected(self, tmp_path):
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="foo", depth=1, mode="nonexistent_mode")
        assert result.is_error
        assert "mode" in result.error.lower() or "nonexistent_mode" in result.error

    def test_valid_modes_accepted(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\n")
        config = _make_config(tmp_path)
        for mode in ("callers", "reads", "call_chain"):
            result = _run(self.tool, config, symbol="foo", depth=1, mode=mode)
            assert not result.is_error, f"mode={mode} should be valid"


# ---------------------------------------------------------------------------
# callers mode
# ---------------------------------------------------------------------------

class TestDataFlowCallersMode:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_callers_finds_direct_call(self, tmp_path):
        """callers mode should find functions that call the target symbol."""
        (tmp_path / "a.py").write_text(
            "def helper():\n    pass\n\ndef caller():\n    helper()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="helper", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["symbol"] == "helper"
        assert data["mode"] == "callers"
        # Should find that caller() calls helper()
        callers = data["results"]
        assert isinstance(callers, list)
        assert len(callers) >= 1
        fn_names = [r.get("enclosing_function") for r in callers]
        assert "caller" in fn_names

    def test_callers_no_match(self, tmp_path):
        """callers mode returns empty results when no callers exist."""
        (tmp_path / "a.py").write_text(
            "def standalone():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="standalone", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["results"] == []

    def test_callers_result_has_file_and_line(self, tmp_path):
        """Each caller result must have file and line fields."""
        (tmp_path / "mod.py").write_text(
            "def target():\n    pass\n\ndef wrapper():\n    target()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="target", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        for r in data["results"]:
            assert "file" in r
            assert "line" in r

    def test_callers_result_has_enclosing_function(self, tmp_path):
        """Each caller result must have enclosing_function field."""
        (tmp_path / "mod2.py").write_text(
            "def target():\n    pass\n\ndef wrapper():\n    target()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="target", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        assert len(data["results"]) >= 1
        for r in data["results"]:
            assert "enclosing_function" in r

    def test_callers_respects_root(self, tmp_path):
        """root parameter restricts the search scope."""
        subdir = tmp_path / "subpkg"
        subdir.mkdir()
        (tmp_path / "outside.py").write_text(
            "def my_func():\n    pass\n\ndef calls_it():\n    my_func()\n"
        )
        (subdir / "inside.py").write_text(
            "def other():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="my_func", depth=1, mode="callers", root="subpkg")
        assert not result.is_error
        data = json.loads(result.output)
        # subpkg doesn't have calls to my_func — should be empty
        assert data["results"] == []

    def test_callers_multiple_callers(self, tmp_path):
        """callers mode finds all callers across multiple functions."""
        (tmp_path / "b.py").write_text(
            "def util():\n    pass\n"
            "def a():\n    util()\n"
            "def b():\n    util()\n"
            "def c():\n    util()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="util", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        fn_names = {r["enclosing_function"] for r in data["results"]}
        assert "a" in fn_names
        assert "b" in fn_names
        assert "c" in fn_names


# ---------------------------------------------------------------------------
# reads mode
# ---------------------------------------------------------------------------

class TestDataFlowReadsMode:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_reads_finds_attribute_access_with_dot(self, tmp_path):
        """reads mode finds obj.attr access when symbol=obj.attr."""
        (tmp_path / "r.py").write_text(
            "def check(cfg):\n    return cfg.max_turns > 5\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="cfg.max_turns", depth=1, mode="reads")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["mode"] == "reads"
        assert len(data["results"]) >= 1
        for r in data["results"]:
            assert "file" in r
            assert "line" in r

    def test_reads_bare_attr_finds_any_access(self, tmp_path):
        """reads mode with bare name (no dot) finds any attribute access with that name."""
        (tmp_path / "r2.py").write_text(
            "def f(a, b):\n    return a.timeout + b.timeout\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="timeout", depth=1, mode="reads")
        assert not result.is_error
        data = json.loads(result.output)
        # Should find both timeout accesses
        assert len(data["results"]) >= 2

    def test_reads_no_matches_returns_empty(self, tmp_path):
        """reads mode returns empty list when attribute is never accessed."""
        (tmp_path / "s.py").write_text(
            "def foo(x):\n    return x.other\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="x.missing_attr", depth=1, mode="reads")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["results"] == []

    def test_reads_result_structure(self, tmp_path):
        """reads mode results have file, line, col, context fields."""
        (tmp_path / "ctx.py").write_text(
            "def f(obj):\n    val = obj.timeout\n    return val\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="obj.timeout", depth=1, mode="reads")
        assert not result.is_error
        data = json.loads(result.output)
        assert len(data["results"]) >= 1
        r = data["results"][0]
        assert "file" in r
        assert "line" in r


# ---------------------------------------------------------------------------
# call_chain mode
# ---------------------------------------------------------------------------

class TestDataFlowCallChainMode:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_call_chain_depth1_has_l1_callers(self, tmp_path):
        """call_chain depth=1 returns l1_callers list."""
        (tmp_path / "chain.py").write_text(
            "def leaf():\n    pass\n"
            "def mid():\n    leaf()\n"
            "def top():\n    mid()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="leaf", depth=1, mode="call_chain")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["mode"] == "call_chain"
        assert isinstance(data["results"], dict)
        assert "l1_callers" in data["results"]
        l1 = data["results"]["l1_callers"]
        assert isinstance(l1, list)
        fn_names = [r.get("enclosing_function") for r in l1]
        assert "mid" in fn_names

    def test_call_chain_depth2_has_l2_callers(self, tmp_path):
        """call_chain depth=2 returns both l1_callers and l2_callers."""
        (tmp_path / "deep.py").write_text(
            "def base():\n    pass\n"
            "def layer1():\n    base()\n"
            "def layer2():\n    layer1()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="base", depth=2, mode="call_chain")
        assert not result.is_error
        data = json.loads(result.output)
        assert "l1_callers" in data["results"]
        assert "l2_callers" in data["results"]
        l2 = data["results"]["l2_callers"]
        assert isinstance(l2, dict)

    def test_call_chain_depth2_finds_second_level(self, tmp_path):
        """call_chain depth=2 discovers callers of callers."""
        (tmp_path / "deep2.py").write_text(
            "def target():\n    pass\n"
            "def middle():\n    target()\n"
            "def outer():\n    middle()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="target", depth=2, mode="call_chain")
        assert not result.is_error
        data = json.loads(result.output)
        l2 = data["results"]["l2_callers"]
        # middle is an l1 caller; outer is an l2 caller (caller of middle)
        assert "middle" in l2
        outer_callers = l2["middle"]
        fn_names = [r.get("enclosing_function") for r in outer_callers]
        assert "outer" in fn_names

    def test_call_chain_no_callers(self, tmp_path):
        """call_chain returns empty l1_callers when nobody calls the symbol."""
        (tmp_path / "lonely.py").write_text(
            "def nobody_calls_me():\n    pass\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="nobody_calls_me", depth=2, mode="call_chain")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["results"]["l1_callers"] == []

    def test_call_chain_depth_capped_at_2(self, tmp_path):
        """depth > 2 is silently capped at 2 (not an error)."""
        (tmp_path / "cap.py").write_text("def f():\n    pass\n")
        config = _make_config(tmp_path)
        # depth=5 should be capped to 2, not raise an error
        result = _run(self.tool, config, symbol="f", depth=5, mode="call_chain")
        assert not result.is_error
        data = json.loads(result.output)
        assert "l1_callers" in data["results"]
        assert "l2_callers" in data["results"]


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------

class TestDataFlowOutputStructure:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_output_is_valid_json(self, tmp_path):
        (tmp_path / "j.py").write_text("def foo():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="foo", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)  # must not raise
        assert isinstance(data, dict)

    def test_output_has_symbol_and_mode_keys(self, tmp_path):
        (tmp_path / "k.py").write_text("def bar():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="bar", depth=1, mode="callers")
        data = json.loads(result.output)
        assert "symbol" in data
        assert "mode" in data
        assert "results" in data

    def test_symbol_reflected_in_output(self, tmp_path):
        (tmp_path / "m.py").write_text("def qux():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="qux", depth=1, mode="callers")
        data = json.loads(result.output)
        assert data["symbol"] == "qux"

    def test_mode_reflected_in_output(self, tmp_path):
        (tmp_path / "n.py").write_text("def quux():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="quux", depth=1, mode="callers")
        data = json.loads(result.output)
        assert data["mode"] == "callers"

    def test_callers_results_is_list(self, tmp_path):
        (tmp_path / "o.py").write_text("def fn():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="fn", depth=1, mode="callers")
        data = json.loads(result.output)
        assert isinstance(data["results"], list)

    def test_reads_results_is_list(self, tmp_path):
        (tmp_path / "p.py").write_text("def fn():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="fn", depth=1, mode="reads")
        data = json.loads(result.output)
        assert isinstance(data["results"], list)

    def test_call_chain_results_is_dict(self, tmp_path):
        (tmp_path / "q.py").write_text("def fn():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="fn", depth=1, mode="call_chain")
        data = json.loads(result.output)
        assert isinstance(data["results"], dict)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestDataFlowEdgeCases:
    def setup_method(self):
        self.tool = DataFlowTool()

    def test_empty_workspace_returns_empty_results(self, tmp_path):
        """Empty workspace returns graceful empty output, not an error."""
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="foo", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        assert data["results"] == []

    def test_multi_file_search(self, tmp_path):
        """callers are found across multiple files."""
        (tmp_path / "x.py").write_text(
            "def shared():\n    pass\n"
        )
        (tmp_path / "y.py").write_text(
            "def use_shared():\n    shared()\n"
        )
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="shared", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        fn_names = {r["enclosing_function"] for r in data["results"]}
        assert "use_shared" in fn_names

    def test_default_mode_is_callers(self, tmp_path):
        """Not passing mode defaults to 'callers'."""
        (tmp_path / "def.py").write_text("def f():\n    pass\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="f", depth=1)
        assert not result.is_error
        data = json.loads(result.output)
        assert data["mode"] == "callers"

    def test_syntax_error_file_skipped_gracefully(self, tmp_path):
        """Files with syntax errors are silently skipped."""
        (tmp_path / "broken.py").write_text("def foo(\n    # broken\n")
        (tmp_path / "good.py").write_text("def foo():\n    pass\ndef caller():\n    foo()\n")
        config = _make_config(tmp_path)
        result = _run(self.tool, config, symbol="foo", depth=1, mode="callers")
        assert not result.is_error
        data = json.loads(result.output)
        # good.py should still be processed
        fn_names = {r["enclosing_function"] for r in data["results"]}
        assert "caller" in fn_names
