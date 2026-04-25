"""Tests for harness/tools/discovery.py (ToolDiscoveryTool)."""

from __future__ import annotations

import asyncio
import json

import pytest

from harness.core.config import HarnessConfig
from harness.tools.discovery import ToolDiscoveryTool
from harness.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def config() -> HarnessConfig:
    return HarnessConfig(workspace=".")


@pytest.fixture()
def config_with_registry() -> HarnessConfig:
    cfg = HarnessConfig(workspace=".")
    reg = ToolRegistry()
    # Populate with a subset of tools
    from harness.tools import DEFAULT_TOOLS
    for tool in DEFAULT_TOOLS:
        reg.register(tool)
    cfg.tool_registry = reg  # type: ignore[attr-defined]
    return cfg


# ---------------------------------------------------------------------------
# Basic attributes
# ---------------------------------------------------------------------------

class TestToolDiscoveryAttributes:
    def test_name(self):
        assert ToolDiscoveryTool().name == "tool_discovery"

    def test_description_is_nonempty(self):
        assert ToolDiscoveryTool().description

    def test_input_schema_has_filter(self):
        schema = ToolDiscoveryTool().input_schema()
        assert "filter" in schema.get("properties", {})

    def test_input_schema_has_tool_name(self):
        schema = ToolDiscoveryTool().input_schema()
        assert "tool_name" in schema.get("properties", {})

    def test_input_schema_has_show_schema(self):
        schema = ToolDiscoveryTool().input_schema()
        assert "show_schema" in schema.get("properties", {})


# ---------------------------------------------------------------------------
# No-registry path — falls back to DEFAULT_TOOLS
# ---------------------------------------------------------------------------

class TestToolDiscoveryNoRegistry:
    def test_no_registry_returns_tool_list(self, config):
        result = run(ToolDiscoveryTool().execute(config))
        assert not result.is_error
        assert result.output

    def test_no_registry_output_is_json(self, config):
        result = run(ToolDiscoveryTool().execute(config))
        assert not result.is_error
        data = json.loads(result.output)
        assert isinstance(data, dict) or isinstance(data, list)

    def test_no_registry_contains_tool_entries(self, config):
        result = run(ToolDiscoveryTool().execute(config))
        data = json.loads(result.output)
        # Result may be dict with 'tools' key or a list
        tools = data.get("tools", data) if isinstance(data, dict) else data
        assert len(tools) > 0


# ---------------------------------------------------------------------------
# Filter mode
# ---------------------------------------------------------------------------

class TestToolDiscoveryFilter:
    def test_filter_reduces_result_set(self, config):
        all_result = run(ToolDiscoveryTool().execute(config, filter=""))
        filtered_result = run(ToolDiscoveryTool().execute(config, filter="git"))
        all_data = json.loads(all_result.output)
        filtered_data = json.loads(filtered_result.output)
        all_tools = all_data.get("tools", all_data) if isinstance(all_data, dict) else all_data
        filtered_tools = filtered_data.get("tools", filtered_data) if isinstance(filtered_data, dict) else filtered_data
        assert len(filtered_tools) < len(all_tools)

    def test_filter_git_returns_git_tools(self, config):
        result = run(ToolDiscoveryTool().execute(config, filter="git"))
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        # All returned tools should have 'git' in name or description
        for tool in tools:
            name = tool.get("name", "").lower()
            desc = tool.get("description", "").lower()
            assert "git" in name or "git" in desc

    def test_filter_case_insensitive(self, config):
        result_lower = run(ToolDiscoveryTool().execute(config, filter="git"))
        result_upper = run(ToolDiscoveryTool().execute(config, filter="GIT"))
        # Both should return the same number of tools
        lower_data = json.loads(result_lower.output)
        upper_data = json.loads(result_upper.output)
        lower_tools = lower_data.get("tools", lower_data) if isinstance(lower_data, dict) else lower_data
        upper_tools = upper_data.get("tools", upper_data) if isinstance(upper_data, dict) else upper_data
        assert len(lower_tools) == len(upper_tools)

    def test_filter_no_match_returns_empty_list(self, config):
        result = run(ToolDiscoveryTool().execute(
            config, filter="xyzzy_no_such_tool_9999"
        ))
        assert not result.is_error
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        assert tools == [] or tools == {}


# ---------------------------------------------------------------------------
# tool_name lookup mode
# ---------------------------------------------------------------------------

class TestToolDiscoveryToolName:
    def test_tool_name_found_returns_schema(self, config):
        result = run(ToolDiscoveryTool().execute(
            config, tool_name="git_status"
        ))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["name"] == "git_status"
        assert "input_schema" in data

    def test_tool_name_not_found_returns_error(self, config):
        result = run(ToolDiscoveryTool().execute(
            config, tool_name="no_such_tool_xyzzy_9999"
        ))
        assert result.is_error
        assert "not found" in result.error.lower()

    def test_tool_name_takes_precedence_over_filter(self, config):
        # tool_name should override filter
        result = run(ToolDiscoveryTool().execute(
            config, tool_name="git_status", filter="bash"
        ))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["name"] == "git_status"

    def test_tool_name_returns_description(self, config):
        result = run(ToolDiscoveryTool().execute(
            config, tool_name="git_status"
        ))
        data = json.loads(result.output)
        assert data.get("description")


# ---------------------------------------------------------------------------
# show_schema mode
# ---------------------------------------------------------------------------

class TestToolDiscoveryShowSchema:
    def test_show_schema_includes_schemas(self, config):
        result = run(ToolDiscoveryTool().execute(config, show_schema=True))
        assert not result.is_error
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        # At least one entry should have an input_schema
        has_schema = any(
            "input_schema" in t
            for t in tools
            if isinstance(t, dict)
        )
        assert has_schema

    def test_show_schema_false_omits_schemas(self, config):
        result = run(ToolDiscoveryTool().execute(config, show_schema=False))
        assert not result.is_error
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        # Compact mode should not include input_schema for every tool
        # (some may have it, but not all should have full schemas)
        all_have_schema = all(
            "input_schema" in t
            for t in tools
            if isinstance(t, dict)
        )
        # In compact mode, not every entry has the full schema
        assert not all_have_schema or len(tools) == 0


# ---------------------------------------------------------------------------
# Registry-backed path
# ---------------------------------------------------------------------------

class TestToolDiscoveryWithRegistry:
    def test_with_registry_returns_results(self, config_with_registry):
        result = run(ToolDiscoveryTool().execute(config_with_registry))
        assert not result.is_error
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        assert len(tools) > 0

    def test_with_registry_filter_works(self, config_with_registry):
        result = run(ToolDiscoveryTool().execute(
            config_with_registry, filter="bash"
        ))
        assert not result.is_error
        data = json.loads(result.output)
        tools = data.get("tools", data) if isinstance(data, dict) else data
        assert len(tools) >= 1
