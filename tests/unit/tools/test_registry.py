"""Tests for harness/tools/registry.py."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult
from harness.tools.registry import (
    ToolRegistry,
    _normalise_params,
    _required_params,
    _check_unknown_params,
    _PARAM_ALIASES,
)


# ---------------------------------------------------------------------------
# Minimal fake tools for testing
# ---------------------------------------------------------------------------

class EchoTool(Tool):
    """Returns whatever is passed as 'message'."""

    name = "echo"
    description = "Echo a message"
    tags: frozenset[str] = frozenset({"testing"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "msg"}},
            "required": ["message"],
        }

    async def execute(self, config: HarnessConfig, *, message: str) -> ToolResult:
        return ToolResult(output=message)


class PermErrorTool(Tool):
    """Always raises PermissionError."""

    name = "perm_error"
    description = "Always raises permission error"

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, config: HarnessConfig) -> ToolResult:
        raise PermissionError("no access")


class BoomTool(Tool):
    """Always raises RuntimeError."""

    name = "boom"
    description = "Always raises RuntimeError"

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, config: HarnessConfig) -> ToolResult:
        raise RuntimeError("something went wrong")


class TaggedTool(Tool):
    """Tool with 'git' and 'analysis' tags."""

    name = "tagged"
    description = "Has tags"
    tags: frozenset[str] = frozenset({"git", "analysis"})

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, config: HarnessConfig) -> ToolResult:
        return ToolResult(output="ok")


@pytest.fixture()
def config(tmp_path) -> HarnessConfig:
    return HarnessConfig(workspace=str(tmp_path))


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(PermErrorTool())
    reg.register(BoomTool())
    reg.register(TaggedTool())
    return reg


# ---------------------------------------------------------------------------
# ToolRegistry.register / get / names
# ---------------------------------------------------------------------------

class TestRegistryBasics:
    def test_register_and_get(self, registry):
        assert registry.get("echo") is not None
        assert registry.get("tagged") is not None

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("no_such_tool") is None

    def test_names_lists_all(self, registry):
        names = registry.names
        assert "echo" in names
        assert "boom" in names
        assert "tagged" in names

    def test_to_api_schema_returns_list(self, registry):
        schemas = registry.to_api_schema()
        assert isinstance(schemas, list)
        assert len(schemas) == 4
        for s in schemas:
            assert "name" in s


# ---------------------------------------------------------------------------
# filter_by_tags
# ---------------------------------------------------------------------------

class TestFilterByTags:
    def test_filter_includes_matching_tool(self, registry):
        filtered = registry.filter_by_tags(frozenset({"git"}))
        assert "tagged" in filtered.names

    def test_filter_excludes_non_matching_tool_with_tags(self, registry):
        filtered = registry.filter_by_tags(frozenset({"git"}))
        # EchoTool has "testing" tag, not "git" — should be excluded
        assert "echo" not in filtered.names

    def test_filter_includes_no_tag_tool(self, registry):
        # BoomTool has empty tags; should always pass through
        filtered = registry.filter_by_tags(frozenset({"git"}))
        assert "boom" in filtered.names

    def test_filter_empty_tags_includes_all(self, registry):
        # Empty frozenset matches nothing — all empty-tag tools still included
        filtered = registry.filter_by_tags(frozenset())
        # Tools with no tags always included
        assert "boom" in filtered.names
        assert "perm_error" in filtered.names


# ---------------------------------------------------------------------------
# ToolRegistry.execute
# ---------------------------------------------------------------------------

class TestRegistryExecute:
    def test_execute_success(self, registry, config):
        result = asyncio.run(registry.execute("echo", config, {"message": "hello"}))
        assert not result.is_error
        assert result.output == "hello"

    def test_execute_unknown_tool(self, registry, config):
        result = asyncio.run(registry.execute("unknown_xyz", config, {}))
        assert result.is_error
        assert "Unknown tool" in result.error

    def test_execute_schema_error_missing_required(self, registry, config):
        # echo requires 'message' — omit it to trigger TypeError
        result = asyncio.run(registry.execute("echo", config, {}))
        assert result.is_error
        assert "SCHEMA ERROR" in result.error
        # Should tell LLM what required params are
        assert "message" in result.error

    def test_execute_unknown_param_rejected(self, registry, config):
        result = asyncio.run(
            registry.execute("echo", config, {"message": "hi", "bogus_param": "x"})
        )
        assert result.is_error
        assert "SCHEMA ERROR" in result.error
        assert "bogus_param" in result.error

    def test_execute_permission_error(self, registry, config):
        result = asyncio.run(registry.execute("perm_error", config, {}))
        assert result.is_error
        assert "PERMISSION ERROR" in result.error

    def test_execute_tool_error(self, registry, config):
        result = asyncio.run(registry.execute("boom", config, {}))
        assert result.is_error
        assert "TOOL ERROR" in result.error
        assert "RuntimeError" in result.error

    def test_allowed_tools_allows_listed_tool(self, registry, tmp_path):
        cfg = HarnessConfig(workspace=str(tmp_path), allowed_tools=["echo"])
        result = asyncio.run(registry.execute("echo", cfg, {"message": "ok"}))
        assert not result.is_error

    def test_allowed_tools_blocks_unlisted_tool(self, registry, tmp_path):
        cfg = HarnessConfig(workspace=str(tmp_path), allowed_tools=["echo"])
        result = asyncio.run(registry.execute("boom", cfg, {}))
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "allowed_tools" in result.error

    def test_empty_allowed_tools_allows_all(self, registry, config):
        # Empty allowed_tools means no restriction
        result = asyncio.run(registry.execute("boom", config, {}))
        assert result.is_error
        assert "TOOL ERROR" in result.error  # got in, tool raised

    def test_elapsed_s_set_on_success(self, registry, config):
        result = asyncio.run(registry.execute("echo", config, {"message": "hi"}))
        assert result.elapsed_s is not None
        assert result.elapsed_s >= 0


# ---------------------------------------------------------------------------
# _normalise_params
# ---------------------------------------------------------------------------

class TestNormaliseParams:
    def test_alias_cmd_to_command(self):
        # BashTool has 'command' in schema. We'll use EchoTool (has 'message') as a proxy
        # instead use a mock-like tool. Let's just test with a real alias case.
        from harness.tools.bash import BashTool
        tool = BashTool()
        result = _normalise_params(tool, {"cmd": "ls"})
        assert result == {"command": "ls"}

    def test_alias_file_content_to_content(self):
        # write_file has 'content' in schema
        from harness.tools.file_write import WriteFileTool
        tool = WriteFileTool()
        result = _normalise_params(tool, {"file_content": "hello", "path": "x.txt"})
        assert result == {"content": "hello", "path": "x.txt"}

    def test_no_clobber_existing_correct_param(self):
        # If 'command' already exists, don't overwrite it with cmd alias
        from harness.tools.bash import BashTool
        tool = BashTool()
        result = _normalise_params(tool, {"cmd": "ls", "command": "echo hi"})
        # 'command' already present — don't overwrite
        assert result["command"] == "echo hi"

    def test_alias_not_applied_when_target_not_in_schema(self):
        # EchoTool has no 'content' or 'path' param, so file_content alias should not apply
        tool = EchoTool()
        result = _normalise_params(tool, {"file_content": "data"})
        # alias target 'content' not in schema, so no rename
        assert "file_content" in result
        assert "content" not in result

    def test_original_dict_not_mutated(self):
        from harness.tools.bash import BashTool
        tool = BashTool()
        original = {"cmd": "ls"}
        result = _normalise_params(tool, original)
        assert original == {"cmd": "ls"}  # original unchanged
        assert result == {"command": "ls"}

    def test_unknown_alias_passthrough(self):
        tool = EchoTool()
        result = _normalise_params(tool, {"totally_unknown_key": "x"})
        assert result == {"totally_unknown_key": "x"}


# ---------------------------------------------------------------------------
# _required_params
# ---------------------------------------------------------------------------

class TestRequiredParams:
    def test_returns_required_list(self):
        tool = EchoTool()
        assert _required_params(tool) == ["message"]

    def test_empty_when_no_required(self):
        tool = BoomTool()
        result = _required_params(tool)
        assert result == []


# ---------------------------------------------------------------------------
# _check_unknown_params
# ---------------------------------------------------------------------------

class TestCheckUnknownParams:
    def test_known_params_returns_none(self):
        tool = EchoTool()
        result = _check_unknown_params(tool, {"message": "hi"})
        assert result is None

    def test_unknown_param_returns_error(self):
        tool = EchoTool()
        result = _check_unknown_params(tool, {"message": "hi", "extra": "x"})
        assert result is not None
        assert result.is_error
        assert "SCHEMA ERROR" in result.error
        assert "extra" in result.error

    def test_empty_params_for_no_required_tool_ok(self):
        tool = BoomTool()
        result = _check_unknown_params(tool, {})
        assert result is None

    def test_multiple_unknown_reported(self):
        tool = EchoTool()
        result = _check_unknown_params(tool, {"message": "hi", "a": 1, "b": 2})
        assert result is not None
        assert "a" in result.error or "b" in result.error


# ---------------------------------------------------------------------------
# _PARAM_ALIASES constant checks
# ---------------------------------------------------------------------------

class TestParamAliases:
    def test_cmd_maps_to_command(self):
        assert _PARAM_ALIASES["cmd"] == "command"

    def test_file_content_maps_to_content(self):
        assert _PARAM_ALIASES["file_content"] == "content"

    def test_file_path_maps_to_path(self):
        assert _PARAM_ALIASES["file_path"] == "path"

    def test_old_string_maps_to_old_str(self):
        assert _PARAM_ALIASES["old_string"] == "old_str"

    def test_new_string_maps_to_new_str(self):
        assert _PARAM_ALIASES["new_string"] == "new_str"
