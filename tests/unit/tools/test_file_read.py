"""Unit tests for harness.tools.file_read."""

import errno
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_read import ReadFileTool


def test_readfile_fallback_path_atomicity():
    """Test that the fallback path for EINVAL uses atomic open+fstat.
    
    Mocks os.open to simulate an EINVAL error on the first call (with O_NOFOLLOW),
    then verifies the fallback path uses os.fstat on the opened file descriptor
    before reading.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track calls to os.open and os.fstat
        open_calls = []
        fstat_calls = []
        
        original_open = os.open
        original_fstat = os.fstat
        
        def mock_open(path, flags, *args, **kwargs):
            open_calls.append((path, flags))
            # First call: simulate EINVAL for O_NOFOLLOW
            if flags & os.O_NOFOLLOW:
                raise OSError(errno.EINVAL, "Invalid argument")
            # Second call: succeed with regular open
            return original_open(path, flags, *args, **kwargs)
        
        def mock_fstat(fd):
            fstat_calls.append(fd)
            return original_fstat(fd)
        
        with patch('os.open', side_effect=mock_open), \
             patch('os.fstat', side_effect=mock_fstat):
            # Run the tool
            result = tool.execute(config, path=str(test_file))
        
        # Verify the fallback path was used correctly
        assert len(open_calls) == 2, f"Expected 2 os.open calls, got {len(open_calls)}"
        
        # First call should have O_NOFOLLOW
        first_path, first_flags = open_calls[0]
        assert str(test_file) in str(first_path)
        assert first_flags & os.O_NOFOLLOW
        
        # Second call should be regular open (no O_NOFOLLOW)
        second_path, second_flags = open_calls[1]
        assert str(test_file) in str(second_path)
        assert not (second_flags & os.O_NOFOLLOW)
        
        # Should have called fstat on the file descriptor
        assert len(fstat_calls) == 1, f"Expected 1 os.fstat call, got {len(fstat_calls)}"
        
        # Result should be successful
        assert not result.is_error
        assert "line1" in result.output


def test_readfile_symlink_protection():
    """Test that symlinks pointing outside allowed directories are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create directory outside workspace
        outside = tmpdir_path / "outside"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("secret content")
        
        # Create symlink in workspace pointing outside
        symlink = workspace / "malicious_link.txt"
        symlink.symlink_to(outside_file)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Should reject the symlink
        result = tool.execute(config, path=str(symlink))
        assert result.is_error
        assert "outside allowed" in result.error.lower() or "symlink" in result.error.lower()


def test_readfile_valid_symlink():
    """Test that valid symlinks within workspace are allowed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create a file inside workspace
        target_file = workspace / "target.txt"
        target_file.write_text("target content\nline2")
        
        # Create a symlink to it
        symlink = workspace / "link.txt"
        symlink.symlink_to(target_file)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Should allow and read through the symlink
        result = tool.execute(config, path=str(symlink))
        assert not result.is_error
        assert "target content" in result.output


def test_readfile_offset_and_limit():
    """Test offset and limit parameters work correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file with multiple lines
        test_file = tmpdir_path / "test.txt"
        content = "\n".join([f"line{i}" for i in range(1, 11)])
        test_file.write_text(content)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Test with offset=3, limit=4
        result = tool.execute(config, path=str(test_file), offset=3, limit=4)
        assert not result.is_error
        output = result.output
        
        # Should show lines 3-6
        assert "line3" in output
        assert "line4" in output
        assert "line5" in output
        assert "line6" in output
        assert "line7" not in output  # Should be excluded by limit
        assert "line2" not in output  # Should be excluded by offset


if __name__ == "__main__":
    pytest.main([__file__, "-v"])