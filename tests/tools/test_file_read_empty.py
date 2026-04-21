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
        
        # Test 1: offset=1 should succeed with formatted header output
        result = await tool.execute(config, path=os.path.basename(temp_path), offset=1, limit=10)
        assert not result.is_error, f"Expected success for offset=1, got error: {result.error}"
        # Empty files should return formatted header matching empty selection format
        assert os.path.basename(temp_path) in result.output, f"Expected filename in output, got: {repr(result.output)}"
        assert "lines 1-0 of 0" in result.output, f"Expected 'lines 1-0 of 0' in output, got: {repr(result.output)}"
        assert result.metadata.get("lines") == [], f"Expected lines metadata to be empty list for empty file, got: {result.metadata}"
        
        # Test 2: offset=2 should fail with appropriate error
        result2 = await tool.execute(config, path=os.path.basename(temp_path), offset=2, limit=10)
        assert result2.is_error, f"Expected error for offset=2"
        assert "Offset 2 exceeds maximum allowed value (1) for file with 0 lines" in result2.error, f"Expected 'Offset 2 exceeds maximum allowed value (1) for file with 0 lines' in error, got: {result2.error}"
        
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


@pytest.mark.asyncio
async def test_read_file_empty_selection_metadata():
    """Test that ReadFileTool returns consistent metadata for empty selection case."""
    
    # Create a temporary file with content
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write('line1\nline2\nline3\n')
        temp_path = f.name
    
    try:
        # Create config with workspace set to temp directory
        config = HarnessConfig(workspace=os.path.dirname(temp_path))
        
        # Create tool instance
        tool = ReadFileTool()
        
        # Mock file_security.atomic_validate_and_read to return the text
        tool.file_security = MagicMock()
        tool.file_security.atomic_validate_and_read = AsyncMock(
            return_value=("line1\nline2\nline3\n", temp_path)
        )
        
        # Test: offset=4 (total + 1) should create empty selection
        result = await tool.execute(config, path=os.path.basename(temp_path), offset=4, limit=10)
        
        # Should not error
        assert not result.is_error, f"Expected success for offset=4 (empty selection), got error: {result.error}"
        
        # Should return metadata with 'lines' key
        assert 'lines' in result.metadata, f"Expected metadata to contain 'lines' key, got: {result.metadata}"
        
        # Lines should be empty list for empty selection
        assert result.metadata['lines'] == [], f"Expected lines metadata to be empty list for empty selection, got: {result.metadata['lines']}"
        
        # Output should contain the filename and line range
        assert os.path.basename(temp_path) in result.output, f"Expected filename in output, got: {result.output}"
        assert "lines 4-3 of 3" in result.output, f"Expected 'lines 4-3 of 3' in output, got: {result.output}"
        
    finally:
        # Clean up
        os.unlink(temp_path)


if __name__ == "__main__":
    # Run tests directly for debugging
    asyncio.run(test_read_file_empty_file_offset_validation())
    asyncio.run(test_read_file_empty_file_integration())
    asyncio.run(test_read_file_empty_selection_metadata())
    print("All tests passed!")