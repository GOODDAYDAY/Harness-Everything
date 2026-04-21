"""Test empty file handling for ReadFileTool."""

import pytest
import tempfile
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock

from harness.tools.file_read import ReadFileTool
from harness.core.config import HarnessConfig


@pytest.mark.asyncio
async def test_read_file_empty_file_offset_validation():
    """Test that ReadFileTool handles empty files correctly."""
    
    # Create a temporary empty file
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write('')
        temp_path = f.name
    
    try:
        # Create config with workspace set to temp directory
        config = HarnessConfig(workspace=os.path.dirname(temp_path))
        
        # Create tool instance
        tool = ReadFileTool()
        
        # Mock file_security.atomic_validate_and_read to return empty text
        # This bypasses the actual file system for unit testing
        tool.file_security = MagicMock()
        tool.file_security.atomic_validate_and_read = AsyncMock(
            return_value=("", temp_path)  # empty text, resolved path
        )
        
        # Test 1: offset=1 should succeed with empty string output
        result = await tool.execute(config, path=os.path.basename(temp_path), offset=1, limit=10)
        assert not result.is_error, f"Expected success for offset=1, got error: {result.error}"
        assert result.output == "", f"Expected empty string output, got: {repr(result.output)}"
        
        # Test 2: offset=2 should fail with appropriate error
        result2 = await tool.execute(config, path=os.path.basename(temp_path), offset=2, limit=10)
        assert result2.is_error, f"Expected error for offset=2"
        assert "only offset=1 allowed" in result2.error, f"Expected 'only offset=1 allowed' in error, got: {result2.error}"
        
        # Test 3: offset=0 should fail with offset validation error
        result3 = await tool.execute(config, path=os.path.basename(temp_path), offset=0, limit=10)
        assert result3.is_error, f"Expected error for offset=0"
        assert "offset must be ≥ 1" in result3.error, f"Expected 'offset must be ≥ 1' in error, got: {result3.error}"
        
    finally:
        # Clean up
        os.unlink(temp_path)


@pytest.mark.asyncio
async def test_read_file_empty_file_integration():
    """Integration test for empty file handling (uses actual file system)."""
    
    # Create a temporary empty file
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write('')
        temp_path = f.name
    
    try:
        # Create config with workspace set to temp directory
        config = HarnessConfig(workspace=os.path.dirname(temp_path))
        
        # Create tool instance
        tool = ReadFileTool()
        
        # Test offset=1 on actual empty file
        result = await tool.execute(config, path=os.path.basename(temp_path), offset=1, limit=10)
        
        # The actual implementation may return different output based on file_security
        # We just verify it doesn't crash and handles the empty file case
        assert not result.is_error, f"Should not error on empty file with offset=1: {result.error}"
        
    finally:
        # Clean up
        os.unlink(temp_path)


if __name__ == "__main__":
    # Run tests directly for debugging
    asyncio.run(test_read_file_empty_file_offset_validation())
    print("All tests passed!")