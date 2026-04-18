"""Tests for the harness tools registry structural integrity.

These checks were previously hardcoded import-time assertions in
harness/tools/__init__.py (as _EXPECTED_DEFAULT_COUNT / _EXPECTED_OPTIONAL_COUNT).
Moving them here keeps the safety net without causing an AssertionError cascade
that breaks all modes on every import when a tool is added or removed.
"""

import asyncio
import tempfile
from unittest.mock import Mock, AsyncMock

# Import core.config first to resolve the shim circular-import ordering issue.
import harness.core.config  # noqa: F401 — side-effect import breaks the circular chain
from harness.tools import DEFAULT_TOOLS, OPTIONAL_TOOLS
from harness.tools.registry import ToolRegistry
from harness.tools.base import Tool, ToolResult
from harness.core.config import HarnessConfig


def test_no_duplicate_tool_names():
    names = [t.name for t in DEFAULT_TOOLS + OPTIONAL_TOOLS]
    assert len(names) == len(set(names)), f"Duplicate tool names: {sorted(names)}"


def test_all_tools_implement_abc():
    from harness.tools.base import Tool

    for tool in DEFAULT_TOOLS + OPTIONAL_TOOLS:
        assert isinstance(tool, Tool), f"{tool!r} is not a Tool instance"
        assert callable(tool.input_schema), f"{tool.name}.input_schema is not callable"
        assert callable(tool.execute), f"{tool.name}.execute is not callable"


def test_tool_registry_basic_operations():
    """Test basic ToolRegistry operations: register, get, names."""
    registry = ToolRegistry()
    
    # Create mock tools
    mock_tool1 = Mock(spec=Tool)
    mock_tool1.name = "test_tool_1"
    mock_tool1.tags = frozenset({"test", "mock"})
    
    mock_tool2 = Mock(spec=Tool)
    mock_tool2.name = "test_tool_2"
    mock_tool2.tags = frozenset({"mock"})
    
    # Test registration
    registry.register(mock_tool1)
    registry.register(mock_tool2)
    
    # Test get
    assert registry.get("test_tool_1") == mock_tool1
    assert registry.get("test_tool_2") == mock_tool2
    assert registry.get("non_existent") is None
    
    # Test names property
    assert set(registry.names) == {"test_tool_1", "test_tool_2"}


def test_tool_registry_filter_by_tags():
    """Test ToolRegistry.filter_by_tags() method."""
    registry = ToolRegistry()
    
    # Create mock tools with different tags
    mock_tool1 = Mock(spec=Tool)
    mock_tool1.name = "tool1"
    mock_tool1.tags = frozenset({"analysis", "search"})
    
    mock_tool2 = Mock(spec=Tool)
    mock_tool2.name = "tool2"
    mock_tool2.tags = frozenset({"file", "io"})
    
    mock_tool3 = Mock(spec=Tool)
    mock_tool3.name = "tool3"
    mock_tool3.tags = frozenset()  # Empty tags
    
    mock_tool4 = Mock(spec=Tool)
    mock_tool4.name = "tool4"
    mock_tool4.tags = frozenset({"analysis", "debug"})
    
    # Register all tools
    for tool in [mock_tool1, mock_tool2, mock_tool3, mock_tool4]:
        registry.register(tool)
    
    # Test filtering by single tag
    filtered = registry.filter_by_tags(frozenset({"analysis"}))
    assert set(filtered.names) == {"tool1", "tool3", "tool4"}  # tool3 has empty tags
    
    # Test filtering by multiple tags (OR logic)
    filtered = registry.filter_by_tags(frozenset({"analysis", "file"}))
    assert set(filtered.names) == {"tool1", "tool2", "tool3", "tool4"}
    
    # Test filtering by non-matching tag
    filtered = registry.filter_by_tags(frozenset({"non_existent"}))
    assert set(filtered.names) == {"tool3"}  # Only tool with empty tags
    
    # Test filtering with empty tags set
    filtered = registry.filter_by_tags(frozenset())
    assert set(filtered.names) == {"tool3"}  # Only tools with empty tags
    
    # Test that filtered registry is independent
    filtered.register(Mock(spec=Tool, name="new_tool", tags=frozenset()))
    assert "new_tool" not in registry.names


def test_tool_registry_execute_routing():
    """Test ToolRegistry.execute() routing and error handling."""
    registry = ToolRegistry()
    
    # Create a mock tool with proper async execute method
    mock_tool = Mock(spec=Tool)
    mock_tool.name = "test_tool"
    mock_tool.tags = frozenset()
    mock_tool.input_schema.return_value = {
        "type": "object",
        "properties": {
            "param1": {"type": "string"},
            "param2": {"type": "integer"}
        },
        "required": ["param1"]
    }
    
    # Mock execute to return a successful result
    mock_result = ToolResult(output='{"result": "success"}')
    mock_tool.execute = AsyncMock(return_value=mock_result)
    
    registry.register(mock_tool)
    
    # Create a minimal config
    config = HarnessConfig(workspace="/tmp/test", allowed_paths=["/tmp/test"])
    
    # Test successful execution
    result = asyncio.run(registry.execute(
        "test_tool",
        config,
        {"param1": "value1", "param2": 42}
    ))
    
    assert not result.is_error
    assert result.output == '{"result": "success"}'
    
    # Verify execute was called with correct arguments
    mock_tool.execute.assert_called_once_with(config, param1="value1", param2=42)


def test_tool_registry_execute_unknown_tool():
    """Test ToolRegistry.execute() with unknown tool name."""
    registry = ToolRegistry()
    config = HarnessConfig(workspace="/tmp/test", allowed_paths=["/tmp/test"])
    
    result = asyncio.run(registry.execute("non_existent_tool", config, {}))
    
    assert result.is_error
    assert "Unknown tool" in result.error


def test_tool_registry_execute_allowed_tools_restriction():
    """Test ToolRegistry.execute() respects config.allowed_tools."""
    registry = ToolRegistry()
    
    # Create mock tools
    mock_tool1 = Mock(spec=Tool)
    mock_tool1.name = "allowed_tool"
    mock_tool1.tags = frozenset()
    mock_tool1.input_schema.return_value = {"type": "object", "properties": {}}
    mock_tool1.execute = AsyncMock(return_value=ToolResult(output="success"))
    
    mock_tool2 = Mock(spec=Tool)
    mock_tool2.name = "blocked_tool"
    mock_tool2.tags = frozenset()
    mock_tool2.input_schema.return_value = {"type": "object", "properties": {}}
    mock_tool2.execute = AsyncMock(return_value=ToolResult(output="success"))
    
    registry.register(mock_tool1)
    registry.register(mock_tool2)
    
    # Config with allowed_tools restriction
    config = HarnessConfig(
        workspace="/tmp/test",
        allowed_paths=["/tmp/test"],
        allowed_tools=["allowed_tool"]
    )
    
    # Test allowed tool
    result = asyncio.run(registry.execute("allowed_tool", config, {}))
    assert not result.is_error
    
    # Test blocked tool
    result = asyncio.run(registry.execute("blocked_tool", config, {}))
    assert result.is_error
    assert result.error == "PERMISSION ERROR: Tool 'blocked_tool' is not in the allowed_tools list."
    assert "PERMISSION ERROR" in result.error
    assert "not in the allowed_tools list" in result.error


def test_tool_registry_execute_allowed_tools_none():
    """Test ToolRegistry.execute() when allowed_tools is None (allow-all)."""
    registry = ToolRegistry()
    
    # Create mock tool
    mock_tool = Mock(spec=Tool)
    mock_tool.name = "test_tool"
    mock_tool.tags = frozenset()
    mock_tool.input_schema.return_value = {"type": "object", "properties": {}}
    mock_tool.execute = AsyncMock(return_value=ToolResult(output="success"))
    
    registry.register(mock_tool)
    
    # Config with allowed_tools=None (allow-all)
    config = HarnessConfig(
        workspace="/tmp/test",
        allowed_paths=["/tmp/test"],
        allowed_tools=None  # Should allow all tools
    )
    
    # Test tool execution should succeed
    result = asyncio.run(registry.execute("test_tool", config, {}))
    assert not result.is_error
    assert result.output == "success"


def test_tool_registry_execute_parameter_normalization():
    """Test ToolRegistry.execute() parameter alias normalization."""
    registry = ToolRegistry()
    
    # Create mock tool
    mock_tool = Mock(spec=Tool)
    mock_tool.name = "test_tool"
    mock_tool.tags = frozenset()
    mock_tool.input_schema.return_value = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}
        },
        "required": ["path", "content"]
    }
    mock_tool.execute = AsyncMock(return_value=ToolResult(output="success"))
    
    registry.register(mock_tool)
    
    config = HarnessConfig(workspace="/tmp/test", allowed_paths=["/tmp/test"])
    
    # Test with aliased parameters (file_path -> path, text -> content)
    result = asyncio.run(registry.execute(
        "test_tool",
        config,
        {"file_path": "/tmp/file.txt", "text": "file content"}
    ))
    
    assert not result.is_error
    
    # Verify execute was called with normalized parameters
    mock_tool.execute.assert_called_once_with(
        config,
        path="/tmp/file.txt",
        content="file content"
    )


def test_tool_registry_execute_error_categories():
    """Test ToolRegistry.execute() error categorization."""
    registry = ToolRegistry()
    config = HarnessConfig(workspace="/tmp/test", allowed_paths=["/tmp/test"])
    
    # Test TypeError -> SCHEMA ERROR
    mock_tool = Mock(spec=Tool)
    mock_tool.name = "test_tool"
    mock_tool.tags = frozenset()
    mock_tool.input_schema.return_value = {
        "type": "object",
        "properties": {
            "required_param": {"type": "string"}
        },
        "required": ["required_param"]
    }
    mock_tool.execute = AsyncMock(side_effect=TypeError("missing required argument"))
    
    registry.register(mock_tool)
    
    result = asyncio.run(registry.execute("test_tool", config, {}))
    assert result.is_error
    assert "SCHEMA ERROR" in result.error
    
    # Test PermissionError -> PERMISSION ERROR
    mock_tool2 = Mock(spec=Tool)
    mock_tool2.name = "test_tool2"
    mock_tool2.tags = frozenset()
    mock_tool2.input_schema.return_value = {"type": "object", "properties": {}}
    mock_tool2.execute = AsyncMock(side_effect=PermissionError("access denied"))
    
    registry2 = ToolRegistry()
    registry2.register(mock_tool2)
    
    result = asyncio.run(registry2.execute("test_tool2", config, {}))
    assert result.is_error
    assert "PERMISSION ERROR" in result.error
    
    # Test generic Exception -> TOOL ERROR
    mock_tool3 = Mock(spec=Tool)
    mock_tool3.name = "test_tool3"
    mock_tool3.tags = frozenset()
    mock_tool3.input_schema.return_value = {"type": "object", "properties": {}}
    mock_tool3.execute = AsyncMock(side_effect=RuntimeError("something went wrong"))
    
    registry3 = ToolRegistry()
    registry3.register(mock_tool3)
    
    result = asyncio.run(registry3.execute("test_tool3", config, {}))
    assert result.is_error
    assert "TOOL ERROR" in result.error
