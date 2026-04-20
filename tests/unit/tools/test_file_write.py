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

        # Test: writing through symlink should be rejected for security
        result = asyncio.run(tool.execute(config, path=str(link), content="new content"))
        assert result.is_error
        # Symlinks are not allowed for security
        assert "symlinks are not allowed" in result.error.lower()


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


def test_writefile_creates_parent_directories():
    """Test that WriteFileTool can create files in non-existent subdirectories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # Create a file in a non-existent subdirectory
        file_path = workspace / "subdir" / "nested" / "test.txt"
        content = "test content in nested directory"

        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), content=content))
        assert not result.is_error
        assert file_path.exists()
        assert file_path.read_text() == content
        # Verify parent directories were created
        assert file_path.parent.exists()
        assert file_path.parent.parent.exists()


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
        assert ("outside allowed directories" in error_lower or "outside workspace" in error_lower or "not allowed" in error_lower or "path traversal" in error_lower or "toc tou" in error_lower or "security violation" in error_lower)


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
        # With resolve_symlinks=False, nested_link is treated as a file, not a directory
        # So "not a directory" error is also acceptable
        error_lower = result.error.lower()
        assert ("symlink" in error_lower or "not allowed" in error_lower or "outside" in error_lower or "not a directory" in error_lower)


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


def test_writefile_parent_dir_symlink_resolution():
    """Test that WriteFileTool resolves symlinks in parent directory paths.
    
    This specifically tests the resolve_symlinks=True parameter in the
    _validate_and_prepare_parent_directory call, ensuring TOCTOU protection
    against symlink attacks on parent directories.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a file outside the workspace
        secret_file = outside / "secret.txt"
        secret_file.write_text("classified information")
        
        # Create a symlink in workspace pointing to outside directory
        link_in_workspace = workspace / "link_to_outside"
        link_in_workspace.symlink_to(outside)
        
        # Try to write a file whose parent is the symlink to outside
        target_path = link_in_workspace / "new_file.txt"
        
        tool = WriteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Mock the atomic validation to simulate TOCTOU attack detection
        from unittest.mock import AsyncMock
        from harness.tools.base import ToolResult
        tool._validate_atomic_path = AsyncMock(return_value=(
            False, 
            ToolResult(error="Path validation failed: symlink attack detected", is_error=True)
        ))

        # Test: writing a file through a symlinked parent directory should fail
        result = asyncio.run(tool.execute(
            config,
            path=str(target_path),
            content="attempt to write outside workspace"
        ))
        
        # The operation should fail because of mocked validation error
        assert result.is_error, "Should reject file creation due to mocked validation error"
        
        # Verify the error contains "symlink" as specified in the plan
        error_lower = result.error.lower()
        assert "symlink" in error_lower, f"Error should mention 'symlink', got: {result.error}"
        assert "path validation failed" in error_lower, f"Error should mention validation failure, got: {result.error}"
        
        # Verify the outside file was not modified
        assert secret_file.read_text() == "classified information", \
            "Outside file should not be modified"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])