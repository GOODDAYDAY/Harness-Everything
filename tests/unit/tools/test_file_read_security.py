"""Security tests for harness.tools.file_read focusing on TOCTOU protection."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_read import ReadFileTool


@pytest.mark.asyncio
async def test_readfile_atomic_open_prevents_symlink_swap():
    """Falsifiable test: ReadFileTool.execute() must fail if file becomes a symlink between validation and open."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create test files
        safe_file = workspace / "safe.txt"
        secret_file = workspace / "secret.txt"
        
        safe_file.write_text("public content")
        secret_file.write_text("secret content")
        
        # Create symlink pointing to safe file initially
        symlink_path = workspace / "link.txt"
        symlink_path.symlink_to(safe_file)
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test 1: Reading through a symlink should fail with atomic validation error
        result = await tool.execute(config, path=str(symlink_path))
        assert result.is_error
        assert "symlinks are not allowed" in result.error.lower()
        
        # Test 2: Replace symlink target after validation would occur
        # In a real attack, an attacker would swap the symlink target
        # between validation and reading. Our atomic open with O_NOFOLLOW
        # should prevent this by rejecting symlinks entirely.
        symlink_path.unlink()
        symlink_path.symlink_to(secret_file)
        
        # Attempt read - should fail because atomic validation rejects symlinks
        result = await tool.execute(config, path=str(symlink_path))
        assert result.is_error
        assert "symlinks are not allowed" in result.error.lower()


@pytest.mark.asyncio
async def test_readfile_atomic_open_handles_broken_symlink():
    """Test that ReadFileTool handles broken symlinks correctly."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a broken symlink
        symlink_path = workspace / "broken_link.txt"
        symlink_path.symlink_to(workspace / "nonexistent.txt")
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Attempt read - should fail because atomic validation rejects symlinks for security
        result = await tool.execute(config, path=str(symlink_path))
        assert result.is_error
        # Should reject symlink with explicit error message
        assert "symlinks are not allowed" in result.error.lower()


@pytest.mark.asyncio
async def test_readfile_atomic_open_protects_against_symlink_race():
    """Test that atomic open prevents symlink TOCTOU race conditions."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a regular file
        regular_file = workspace / "regular.txt"
        regular_file.write_text("regular content")
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test reading a regular file (not a symlink)
        result = await tool.execute(config, path=str(regular_file))
        assert not result.is_error
        assert "regular content" in result.output
        
        # Now replace the file with a symlink to outside workspace
        outside_dir = Path(tmpdir) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "outside.txt"
        outside_file.write_text("outside content")
        
        # Replace the regular file with a symlink
        regular_file.unlink()
        regular_file.symlink_to(outside_file)
        
        # Attempt to read - should fail because symlink points outside workspace
        result = await tool.execute(config, path=str(regular_file))
        assert result.is_error
        # Should be blocked by path validation


@pytest.mark.asyncio
async def test_readfile_respects_max_lines_limit():
    """Test that ReadFileTool enforces MAX_READ_LINES limit."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a file with many lines
        file_path = workspace / "bigfile.txt"
        lines = ["Line {}\n".format(i) for i in range(20000)]
        file_path.write_text("".join(lines))
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test with limit exceeding MAX_READ_LINES
        result = await tool.execute(config, path=str(file_path), limit=15000)
        assert result.is_error
        assert "exceeds maximum allowed lines" in result.error
        
        # Test with valid limit
        result = await tool.execute(config, path=str(file_path), limit=5000)
        assert not result.is_error
        # Should read first 5000 lines


@pytest.mark.asyncio
async def test_readfile_offset_beyond_file_length():
    """Test that ReadFileTool handles offset beyond file length correctly."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a file with 5 lines
        file_path = workspace / "test.txt"
        file_path.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test with offset beyond file length - should now return an error
        result = await tool.execute(config, path=str(file_path), offset=10, limit=5)
        assert result.is_error
        assert "Offset 10 exceeds file length (5 lines)" in result.error
        
        # Test with offset exactly at end (line 6, file has 5 lines) - should also return error
        result2 = await tool.execute(config, path=str(file_path), offset=6, limit=5)
        assert result2.is_error
        assert "Offset 6 exceeds file length (5 lines)" in result2.error
        
        # Test with offset within file for comparison
        result3 = await tool.execute(config, path=str(file_path), offset=2, limit=2)
        assert not result3.is_error
        assert len(result3.metadata["lines"]) == 2
        assert result3.metadata["lines"][0][0] == 2  # line number
        assert result3.metadata["lines"][0][1] == "Line 2\n"  # line content
        assert "lines 2-3 of 5" in result3.output


@pytest.mark.asyncio
async def test_readfile_empty_file_offset_handling():
    """Test that ReadFileTool correctly handles offset=1 on empty files."""
    tool = ReadFileTool()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create an empty file
        file_path = workspace / "empty.txt"
        file_path.write_text("")  # Empty file
        
        # Create mock config
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test with offset=1 on empty file - should now succeed with empty output
        result = await tool.execute(config, path=str(file_path), offset=1, limit=10)
        assert not result.is_error
        assert "[empty.txt] lines 1-0 of 0" in result.output
        
        # Also test with offset=1 explicitly (default)
        result2 = await tool.execute(config, path=str(file_path), limit=10)
        assert not result2.is_error
        assert "[empty.txt] lines 1-0 of 0" in result2.output
        
        # Test offset=2 on empty file should fail
        result3 = await tool.execute(config, path=str(file_path), offset=2, limit=10)
        assert result3.is_error
        assert "Offset 2 exceeds file length (0 lines)" in result3.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])