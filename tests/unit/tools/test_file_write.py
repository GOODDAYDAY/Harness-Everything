"""Unit tests for harness.tools.file_write."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_write import WriteFileTool


def test_writefile_atomic_symlink_protection():
    """Test that WriteFileTool prevents TOCTOU symlink attacks with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        legit = workspace / "data.txt"
        legit.write_text("safe")
        secret = outside / "secret.txt"
        secret.write_text("classified")

        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected
        result = asyncio.run(tool.execute(config, path=str(link), content="new content"))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_writefile_valid_file():
    """Test that WriteFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        content = "test content\nwith multiple lines"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=content))
        assert not result.is_error
        assert file_path.exists()
        assert file_path.read_text() == content


def test_writefile_new_file():
    """Test that WriteFileTool creates new files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "new_file.txt"
        content = "new file content"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=content))
        assert not result.is_error
        assert file_path.exists()
        assert file_path.read_text() == content


def test_writefile_overwrite_existing():
    """Test that WriteFileTool overwrites existing files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "existing.txt"
        file_path.write_text("old content")
        
        new_content = "new content"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=new_content))
        assert not result.is_error
        assert file_path.read_text() == new_content


def test_writefile_creates_parent_directories():
    """Test that WriteFileTool creates parent directories if needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "deep" / "nested" / "file.txt"
        content = "test content"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=content))
        assert not result.is_error
        assert file_path.exists()
        assert file_path.read_text() == content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])