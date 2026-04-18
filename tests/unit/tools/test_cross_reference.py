"""Unit tests for the cross_reference tool."""

import asyncio
from pathlib import Path
import pytest
from harness.tools.cross_reference import CrossReferenceTool
from harness.core.config import HarnessConfig


def test_execute_rejects_invalid_symbol_depth(tmp_path):
    """Test that CrossReferenceTool.execute() rejects symbols exceeding maximum depth.
    
    This test validates the falsifiable criterion: the tool must produce a measurable
    security improvement by rejecting deeply nested symbols that could cause DoS attacks.
    """
    # Create a temporary workspace
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = str(workspace_path)
    
    # Create a simple Python file to search
    test_file = workspace_path / "test.py"
    test_file.write_text("""
def some_function():
    pass
""")
    
    # Create config
    config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
    
    # Create the tool
    tool = CrossReferenceTool()
    
    # Test: Symbol exceeding maximum depth (10 dots, 11 identifiers)
    # This should pass regex but fail depth validation
    max_depth_symbol = "a.b.c.d.e.f.g.h.i.j.k"  # 10 dots, 11 identifiers
    
    result = asyncio.run(tool.execute(
        config,
        symbol=max_depth_symbol,
        root=workspace
    ))
    
    # The result should be an error because it exceeds _MAX_SYMBOL_DEPTH
    assert result.is_error, f"Symbol exceeding maximum depth should trigger error: {max_depth_symbol}"
    assert "Symbol validation failed" in result.error, \
        f"Error should mention symbol validation. Got: {result.error}"
    
    # Verify the error contains the exact phrase about exceeding maximum identifier count
    assert "exceeds maximum identifier count" in result.error, \
        f"Error should contain 'exceeds maximum identifier count'. Got: {result.error}"
    assert "11" in result.error and "10" in result.error, \
        f"Error should mention actual count (11) and limit (10). Got: {result.error}"
    
    # Test: Valid symbol with maximum allowed depth (9 dots, 10 identifiers)
    valid_max_depth_symbol = "a.b.c.d.e.f.g.h.i.j"  # 9 dots, 10 identifiers
    
    result = asyncio.run(tool.execute(
        config,
        symbol=valid_max_depth_symbol,
        root=workspace
    ))
    
    # This should not be an error (though it may not find the symbol)
    # We just verify it doesn't fail with validation error
    if result.is_error:
        # If it's an error, it shouldn't be a validation error
        assert "Symbol validation failed" not in result.error, \
            f"Valid symbol at max depth should not trigger validation error. Got: {result.error}"