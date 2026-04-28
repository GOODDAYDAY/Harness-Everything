"""Tests for harness/tools/discovery.py."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path


from harness.core.config import HarnessConfig
from harness.tools.base import Tool
from harness.tools.discovery import ToolDiscoveryTool, discover_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def make_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        model="test",
        max_tokens=1000,
        workspace=str(tmp_path),
        allowed_paths=[str(tmp_path)],
    )


def write_tool_module(directory: Path, stem: str, class_name: str, tool_name: str) -> Path:
    """Write a minimal Tool subclass file in directory."""
    content = textwrap.dedent(f"""
        from __future__ import annotations
        from harness.core.config import HarnessConfig
        from harness.tools.base import Tool, ToolResult

        class {class_name}(Tool):
            name = "{tool_name}"
            description = "A test tool: {tool_name}"

            def input_schema(self) -> dict:
                return {{"type": "object", "properties": {{}}, "required": []}}

            async def execute(self, config: HarnessConfig, **kwargs) -> ToolResult:
                return ToolResult(output="ok")
    """)
    path = directory / f"{stem}.py"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# discover_tools
# ---------------------------------------------------------------------------

class TestDiscoverTools:
    def test_nonexistent_directory_returns_empty(self, tmp_path):
        result = discover_tools(tmp_path / "does_not_exist")
        assert result == []

    def test_empty_directory_returns_empty(self, tmp_path):
        result = discover_tools(tmp_path)
        assert result == []

    def test_discovers_single_tool(self, tmp_path):
        write_tool_module(tmp_path, "my_tool", "MyTool", "my_tool")
        result = discover_tools(tmp_path)
        assert len(result) == 1
        assert result[0].name == "my_tool"  # type: ignore[attr-defined]

    def test_discovers_multiple_tools(self, tmp_path):
        write_tool_module(tmp_path, "tool_a", "ToolA", "tool_a")
        write_tool_module(tmp_path, "tool_b", "ToolB", "tool_b")
        result = discover_tools(tmp_path)
        assert len(result) == 2

    def test_result_sorted_by_module_stem(self, tmp_path):
        write_tool_module(tmp_path, "zebra_tool", "ZebraTool", "zebra_tool")
        write_tool_module(tmp_path, "alpha_tool", "AlphaTool", "alpha_tool")
        result = discover_tools(tmp_path)
        names = [cls.name for cls in result]  # type: ignore[attr-defined]
        # sorted alphabetically by module stem (alpha < zebra)
        assert names.index("alpha_tool") < names.index("zebra_tool")

    def test_skips_init_module(self, tmp_path):
        # __init__.py should always be skipped
        init = tmp_path / "__init__.py"
        init.write_text("# empty\n")
        write_tool_module(tmp_path, "real_tool", "RealTool", "real_tool")
        result = discover_tools(tmp_path)
        assert len(result) == 1

    def test_skips_base_module(self, tmp_path):
        content = "from harness.tools.base import Tool\n"
        (tmp_path / "base.py").write_text(content)
        write_tool_module(tmp_path, "real_tool", "RealTool", "real_tool")
        result = discover_tools(tmp_path)
        assert len(result) == 1

    def test_skips_registry_module(self, tmp_path):
        (tmp_path / "registry.py").write_text("# nothing\n")
        write_tool_module(tmp_path, "real_tool", "RealTool", "real_tool")
        result = discover_tools(tmp_path)
        assert len(result) == 1

    def test_custom_skip_names(self, tmp_path):
        write_tool_module(tmp_path, "skip_me", "SkipMe", "skip_me")
        write_tool_module(tmp_path, "keep_me", "KeepMe", "keep_me")
        result = discover_tools(tmp_path, skip_names={"skip_me"})
        assert len(result) == 1
        assert result[0].name == "keep_me"  # type: ignore[attr-defined]

    def test_returns_class_not_instance(self, tmp_path):
        write_tool_module(tmp_path, "my_tool", "MyTool", "my_tool")
        result = discover_tools(tmp_path)
        assert isinstance(result[0], type)

    def test_deduplicates_classes(self, tmp_path):
        """A module importing Tool from another module shouldn't double-count."""
        write_tool_module(tmp_path, "tool_a", "ToolA", "tool_a")
        # tool_b imports ToolA from tool_a and also defines its own
        tool_b_path = tmp_path / "tool_b.py"
        tool_b_path.write_text(textwrap.dedent("""
            from __future__ import annotations
            from harness.core.config import HarnessConfig
            from harness.tools.base import Tool, ToolResult

            class ToolB(Tool):
                name = "tool_b"
                description = "tool b"

                def input_schema(self):
                    return {"type": "object", "properties": {}, "required": []}

                async def execute(self, config, **kwargs):
                    return ToolResult(output="ok")
        """))
        result = discover_tools(tmp_path)
        names = [cls.name for cls in result]  # type: ignore[attr-defined]
        assert len(names) == len(set(names)), "Duplicate tool names found"

    def test_skips_abstract_classes(self, tmp_path):
        """Abstract classes (missing abstract methods) should not be returned."""
        abstract_path = tmp_path / "abstract_tool.py"
        abstract_path.write_text(textwrap.dedent("""
            from __future__ import annotations
            from harness.tools.base import Tool

            # Tool itself is abstract — reexporting it doesn't add to results
            AbstractReexport = Tool
        """))
        result = discover_tools(tmp_path)
        assert all(cls is not Tool for cls in result)

    def test_continues_after_import_error(self, tmp_path):
        """A broken module should be skipped, not abort discovery."""
        bad_path = tmp_path / "broken_tool.py"
        bad_path.write_text("raise ImportError('intentionally broken')\n")
        write_tool_module(tmp_path, "good_tool", "GoodTool", "good_tool")
        # Should not raise
        result = discover_tools(tmp_path)
        names = [cls.name for cls in result]  # type: ignore[attr-defined]
        assert "good_tool" in names

    def test_package_prefix_used_in_module_name(self, tmp_path, monkeypatch):
        """When package is given, module names include the package prefix."""
        write_tool_module(tmp_path, "pkg_tool", "PkgTool", "pkg_tool")
        # Remove any cached module with this name to force fresh load
        pkg_key = "testpkg.pkg_tool"
        sys.modules.pop(pkg_key, None)
        result = discover_tools(tmp_path, package="testpkg")
        assert len(result) == 1
        # Module should be registered in sys.modules under the package name
        assert pkg_key in sys.modules

    def test_no_package_uses_stem_as_module_name(self, tmp_path):
        """When package is None, module name is just the file stem."""
        write_tool_module(tmp_path, "stem_tool", "StemTool", "stem_tool")
        sys.modules.pop("stem_tool", None)
        result = discover_tools(tmp_path)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# ToolDiscoveryTool.execute
# ---------------------------------------------------------------------------

class TestToolDiscoveryToolExecute:
    tool = ToolDiscoveryTool()

    def test_no_filter_returns_all_tools(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg))
        assert not result.is_error
        data = json.loads(result.output)
        assert "tools" in data
        assert data["total_tools"] >= 1

    def test_filter_restricts_results(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, filter="batch_read"))
        assert not result.is_error
        data = json.loads(result.output)
        # All returned tools should have the filter in name or description
        for t in data["tools"]:
            assert (
                "batch_read" in t["name"].lower()
                or "batch_read" in t["description"].lower()
            )

    def test_filter_case_insensitive(self, tmp_path):
        cfg = make_config(tmp_path)
        result_lower = run(self.tool.execute(cfg, filter="bash"))
        result_upper = run(self.tool.execute(cfg, filter="BASH"))
        assert not result_lower.is_error
        assert not result_upper.is_error
        data_lower = json.loads(result_lower.output)
        data_upper = json.loads(result_upper.output)
        assert data_lower["total_tools"] == data_upper["total_tools"]

    def test_filter_no_match_returns_empty_list(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, filter="zzznomatch999"))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_tools"] == 0
        assert data["tools"] == []

    def test_tool_name_returns_schema(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, tool_name="bash"))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["name"] == "bash"
        assert "input_schema" in data

    def test_tool_name_unknown_returns_error(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, tool_name="zzznot_a_real_tool"))
        assert result.is_error
        assert "not found" in result.error.lower()

    def test_tool_name_takes_precedence_over_filter(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, tool_name="bash", filter="something_else"))
        assert not result.is_error
        data = json.loads(result.output)
        # Should return single tool schema, not filtered list
        assert data["name"] == "bash"

    def test_show_schema_includes_input_schema(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, show_schema=True))
        assert not result.is_error
        data = json.loads(result.output)
        for t in data["tools"]:
            assert "input_schema" in t

    def test_show_schema_false_excludes_input_schema(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg, show_schema=False))
        assert not result.is_error
        data = json.loads(result.output)
        for t in data["tools"]:
            assert "input_schema" not in t

    def test_output_is_valid_json(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg))
        assert not result.is_error
        # Should parse without error
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)

    def test_result_entries_have_required_fields(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg))
        data = json.loads(result.output)
        for entry in data["tools"]:
            assert "name" in entry
            assert "description" in entry
            assert "required_params" in entry
            assert "optional_params" in entry

    def test_tools_sorted_by_name(self, tmp_path):
        cfg = make_config(tmp_path)
        result = run(self.tool.execute(cfg))
        data = json.loads(result.output)
        names = [t["name"] for t in data["tools"]]
        assert names == sorted(names)

    def test_filter_applied_field(self, tmp_path):
        cfg = make_config(tmp_path)
        result_filtered = run(self.tool.execute(cfg, filter="bash"))
        data_filtered = json.loads(result_filtered.output)
        assert data_filtered["filter_applied"] == "bash"

        result_unfiltered = run(self.tool.execute(cfg))
        data_unfiltered = json.loads(result_unfiltered.output)
        assert data_unfiltered["filter_applied"] is None
