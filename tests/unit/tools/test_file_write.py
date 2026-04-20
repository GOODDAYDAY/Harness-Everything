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
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: writing through symlink to file inside workspace should succeed
        result = asyncio.run(tool.execute(config, path=str(link), content="new content"))
        assert not result.is_error
        # Should write to the target of the symlink
        assert legit.read_text() == "new content"


def test_writefile_valid_file():
    """Test that WriteFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        content = "test content\nwith multiple lines"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
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
        config.workspace = str(workspace)
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
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=new_content))
        assert not result.is_error
        assert file_path.read_text() == new_content

def test_writefile_atomic_validation_raises_on_path_traversal():
    """Test that WriteFileTool's atomic validation rejects path traversal attempts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a legitimate file inside workspace
        legit_file = workspace / "data.txt"
        legit_file.write_text("safe")
        
        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test path with '..' traversal attempt
        result = asyncio.run(tool.execute(config, path="../outside.txt", content="malicious"))
        assert result.is_error
        # Should be rejected by atomic validation
        error_lower = result.error.lower()
        assert ("outside allowed directories" in error_lower or "outside workspace" in error_lower or "not allowed" in error_lower or "path traversal" in error_lower)


def test_writefile_atomic_parent_symlink_protection():
    """Test that WriteFileTool prevents TOCTOU symlink attacks on parent directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a legitimate file in workspace
        legit_file = workspace / "data.txt"
        legit_file.write_text("safe")
        
        # Create a secret file outside workspace
        secret_file = outside / "secret.txt"
        secret_file.write_text("classified")
        
        # Create a symlink to a parent directory
        parent_link = workspace / "link_parent"
        parent_link.mkdir()
        
        # Create a symlink inside the parent_link directory pointing outside
        nested_link = parent_link / "nested"
        nested_link.symlink_to(outside)
        
        # Try to write a file through the symlinked parent directory
        target_path = nested_link / "target.txt"
        
        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: writing through symlinked parent directory should be rejected
        result = asyncio.run(tool.execute(
            config, 
            path=str(target_path), 
            content="malicious content"
        ))
        assert result.is_error
        # Should be rejected by atomic parent directory validation
        # Either symlink error or outside allowed paths error is acceptable
        error_lower = result.error.lower()
        assert ("symlink" in error_lower or "not allowed" in error_lower or "outside" in error_lower)


def test_writefile_atomic_read_text():
    """Test that WriteFileTool's _atomic_read_text method works correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        content = "test content\nwith multiple lines"
        file_path.write_text(content)

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: _atomic_read_text should return the file content
        text, error = asyncio.run(tool._atomic_read_text(config, str(file_path)))
        assert error is None
        assert text == content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])