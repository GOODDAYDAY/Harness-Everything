"""Unit tests for harness.tools.file_ops."""

import asyncio
import errno
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_ops import CopyFileTool, DeleteFileTool, MoveFileTool


def test_copyfile_atomic_symlink_protection():
    """Test that CopyFileTool prevents TOCTOU symlink attacks with atomic open."""
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

        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected
        result = asyncio.run(tool.execute(config, source=str(link), destination=str(workspace / "copy.txt")))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_deletefile_atomic_symlink_protection():
    """Test that DeleteFileTool prevents TOCTOU symlink attacks with atomic open."""
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

        tool = DeleteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected
        result = asyncio.run(tool.execute(config, path=str(link)))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_movefile_atomic_symlink_protection():
    """Test that MoveFileTool prevents TOCTOU symlink attacks with atomic open."""
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

        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected
        result = asyncio.run(tool.execute(config, source=str(link), destination=str(workspace / "moved.txt")))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_copyfile_valid_file():
    """Test that CopyFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"

        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
        assert not result.is_error
        assert destination.exists()
        assert destination.read_text() == "test content"


def test_deletefile_valid_file():
    """Test that DeleteFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_to_delete = workspace / "file.txt"
        file_to_delete.write_text("test content")

        tool = DeleteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_to_delete)))
        assert not result.is_error
        assert not file_to_delete.exists()


def test_movefile_valid_file():
    """Test that MoveFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"

        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
        assert not result.is_error
        assert not source.exists()
        assert destination.exists()
        assert destination.read_text() == "test content"


def test_copyfile_source_not_found():
    """Test that CopyFileTool handles missing source file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        source = workspace / "nonexistent.txt"
        destination = workspace / "dest.txt"

        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
        assert result.is_error
        assert "not found" in result.error.lower()


def test_deletefile_not_found():
    """Test that DeleteFileTool handles missing file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_to_delete = workspace / "nonexistent.txt"

        tool = DeleteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_to_delete)))
        assert result.is_error
        assert "not found" in result.error.lower()


def test_movefile_source_not_found():
    """Test that MoveFileTool handles missing source file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        source = workspace / "nonexistent.txt"
        destination = workspace / "dest.txt"

        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
        assert result.is_error
        assert "not found" in result.error.lower()


def test_movefile_atomic_symlink_protection_destination():
    """Test that MoveFileTool prevents TOCTOU symlink attacks on destination with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a legitimate source file inside workspace
        source = workspace / "source.txt"
        source.write_text("safe content")
        
        # Create a legitimate file inside workspace
        legit = workspace / "data.txt"
        legit.write_text("safe")
        
        # Create a secret file outside workspace
        secret = outside / "secret.txt"
        secret.write_text("classified")

        # Create a symlink in workspace pointing to legit file
        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: destination is a symlink - should be rejected
        result = asyncio.run(tool.execute(config, source=str(source), destination=str(link)))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_copyfile_atomic_symlink_protection_destination():
    """Test that CopyFileTool prevents TOCTOU symlink attacks on destination with atomic open."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a legitimate source file inside workspace
        source = workspace / "source.txt"
        source.write_text("safe content")
        
        # Create a legitimate file inside workspace
        legit = workspace / "data.txt"
        legit.write_text("safe")
        
        # Create a secret file outside workspace
        secret = outside / "secret.txt"
        secret.write_text("classified")

        # Create a symlink in workspace pointing to legit file
        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: destination is a symlink - should be rejected
        result = asyncio.run(tool.execute(config, source=str(source), destination=str(link)))
        assert result.is_error
        assert "symlink" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])