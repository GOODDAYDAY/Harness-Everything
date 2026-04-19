"""Unit tests for harness.tools.file_edit."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_edit import EditFileTool


def test_editfile_atomic_symlink_protection():
    """Test that EditFileTool prevents TOCTOU symlink attacks with atomic open."""
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

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected
        result = asyncio.run(tool.execute(config, path=str(link), old_str="safe", new_str="unsafe"))
        assert result.is_error
        assert "symlink" in result.error.lower()


def test_editfile_valid_replacement():
    """Test that EditFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\nhello again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings"))
        assert not result.is_error
        assert file_path.read_text() == "greetings world\nthis is a test\ngreetings again"


def test_editfile_single_replacement():
    """Test that EditFileTool replaces only first occurrence by default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\nhello again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings", replace_all=False))
        assert not result.is_error
        assert file_path.read_text() == "greetings world\nthis is a test\nhello again"


def test_editfile_all_replacements():
    """Test that EditFileTool replaces all occurrences when replace_all=True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\nhello again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings", replace_all=True))
        assert not result.is_error
        assert file_path.read_text() == "greetings world\nthis is a test\ngreetings again"


def test_editfile_old_str_not_found():
    """Test that EditFileTool handles old_str not found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="nonexistent", new_str="replacement"))
        assert result.is_error
        assert "not found" in result.error.lower()


def test_editfile_multiple_occurrences_error():
    """Test that EditFileTool errors when multiple occurrences and replace_all=False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nhello again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings", replace_all=False))
        assert result.is_error
        assert "appears" in result.error.lower()
        assert "replace_all=true" in result.error.lower()


def test_editfile_file_not_found():
    """Test that EditFileTool handles missing file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "nonexistent.txt"

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="test", new_str="replacement"))
        assert result.is_error
        assert "not found" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])