"""Unit tests for harness.tools.directory."""

import asyncio
import errno
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.core.config import HarnessConfig
from harness.tools.directory import ListDirectoryTool, CreateDirectoryTool, TreeTool


def test_listdirectory_atomic_symlink_protection():
    """Test that ListDirectoryTool prevents TOCTOU symlink attacks with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        legit = workspace / "data.txt"
        legit.write_text("safe content")
        secret = outside / "secret.txt"
        secret.write_text("classified")

        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = ListDirectoryTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected by atomic validation
        # Mock os.open to simulate symlink detection
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(link)))
            assert result.is_error
            assert "symlink" in result.error.lower()


def test_listdirectory_valid_directory():
    """Test that ListDirectoryTool works correctly with regular directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = (Path(tmpdir) / "workspace").resolve()
        workspace.mkdir()
        
        # Create some test files and directories
        file1 = workspace / "file1.txt"
        file1.write_text("content1")
        subdir = workspace / "subdir"
        subdir.mkdir()
        file2 = workspace / "file2.txt"
        file2.write_text("content2")

        tool = ListDirectoryTool()
        config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])

        result = asyncio.run(tool.execute(config, path=str(workspace)))
        assert not result.is_error
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output
        assert "[dir]  subdir/" in result.output


def test_createdirectory_atomic_symlink_protection():
    """Test that CreateDirectoryTool prevents TOCTOU symlink attacks with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        legit = workspace / "data.txt"
        legit.write_text("safe content")
        secret = outside / "secret.txt"
        secret.write_text("classified")

        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = CreateDirectoryTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected by atomic validation
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(link)))
            assert result.is_error
            assert "symlink" in result.error.lower()


def test_createdirectory_valid_creation():
    """Test that CreateDirectoryTool creates directories correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = (Path(tmpdir) / "workspace").resolve()
        workspace.mkdir()

        new_dir = workspace / "new_directory"

        tool = CreateDirectoryTool()
        config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])

        result = asyncio.run(tool.execute(config, path=str(new_dir)))
        assert not result.is_error
        assert new_dir.exists()
        assert new_dir.is_dir()


def test_createdirectory_nested_creation():
    """Test that CreateDirectoryTool creates nested directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = (Path(tmpdir) / "workspace").resolve()
        workspace.mkdir()

        nested_dir = workspace / "deep" / "nested" / "directory"

        tool = CreateDirectoryTool()
        config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])

        result = asyncio.run(tool.execute(config, path=str(nested_dir)))
        assert not result.is_error
        assert nested_dir.exists()
        assert nested_dir.is_dir()


def test_tree_atomic_symlink_protection():
    """Test that TreeTool prevents TOCTOU symlink attacks with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        legit = workspace / "data.txt"
        legit.write_text("safe content")
        secret = outside / "secret.txt"
        secret.write_text("classified")

        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = TreeTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected by atomic validation
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(link)))
            assert result.is_error
            assert "symlink" in result.error.lower()


def test_tree_valid_directory():
    """Test that TreeTool works correctly with regular directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = (Path(tmpdir) / "workspace").resolve()
        workspace.mkdir()
        
        # Create a test directory structure
        file1 = workspace / "file1.txt"
        file1.write_text("content1")
        subdir = workspace / "subdir"
        subdir.mkdir()
        file2 = subdir / "file2.txt"
        file2.write_text("content2")

        tool = TreeTool()
        config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])

        result = asyncio.run(tool.execute(config, path=str(workspace)))
        assert not result.is_error
        assert "workspace/" in result.output
        assert "file1.txt" in result.output
        assert "subdir/" in result.output


def test_tree_max_depth():
    """Test that TreeTool respects max_depth parameter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = (Path(tmpdir) / "workspace").resolve()
        workspace.mkdir()
        
        # Create a nested directory structure
        level1 = workspace / "level1"
        level1.mkdir()
        level2 = level1 / "level2"
        level2.mkdir()
        level3 = level2 / "level3"
        level3.mkdir()

        tool = TreeTool()
        config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])

        # Test with max_depth=1
        result = asyncio.run(tool.execute(config, path=str(workspace), max_depth=1))
        assert not result.is_error
        assert "level1/" in result.output
        assert "level2/" not in result.output  # Should not appear due to depth limit

        # Test with max_depth=2
        result = asyncio.run(tool.execute(config, path=str(workspace), max_depth=2))
        assert not result.is_error
        assert "level1/" in result.output
        assert "level2/" in result.output
        assert "level3/" not in result.output  # Should not appear due to depth limit


def test_directory_tools_use_atomic_validation():
    """Test that all directory tools use atomic validation for symlink protection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a valid directory
        valid_dir = workspace / "valid_dir"
        valid_dir.mkdir()
        
        # Test ListDirectoryTool with mocked os.open to simulate symlink attack
        tool = ListDirectoryTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Mock os.open to raise ELOOP error (symlink detection)
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(valid_dir)))
            assert result.is_error
            assert "symlink" in result.error.lower()
        
        # Test CreateDirectoryTool with mocked os.open
        tool = CreateDirectoryTool()
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(valid_dir)))
            assert result.is_error
            assert "symlink" in result.error.lower()
        
        # Test TreeTool with mocked os.open
        tool = TreeTool()
        with patch('os.open') as mock_open:
            mock_open.side_effect = OSError(errno.ELOOP, "Too many levels of symbolic links")
            result = asyncio.run(tool.execute(config, path=str(valid_dir)))
            assert result.is_error
            assert "symlink" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
