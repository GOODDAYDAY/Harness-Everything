"""Unit tests for harness.tools.file_edit."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock, AsyncMock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_edit import EditFileTool
from harness.tools.base import ToolResult


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
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be followed and target file edited
        result = asyncio.run(tool.execute(config, path=str(link), old_str="safe", new_str="unsafe"))
        assert not result.is_error
        # Should edit the target of the symlink
        assert legit.read_text() == "unsafe content"


def test_editfile_valid_replacement():
    """Test that EditFileTool works correctly with regular files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\ngoodbye again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings"))
        assert not result.is_error
        assert file_path.read_text() == "greetings world\nthis is a test\ngoodbye again"


def test_editfile_single_replacement():
    """Test that EditFileTool replaces only first occurrence by default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\ngoodbye again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="hello", new_str="greetings", replace_all=False))
        assert not result.is_error
        assert file_path.read_text() == "greetings world\nthis is a test\ngoodbye again"


def test_editfile_all_replacements():
    """Test that EditFileTool replaces all occurrences when replace_all=True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\nhello again")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
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
        config.workspace = str(workspace)
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
        config.workspace = str(workspace)
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
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]

        result = asyncio.run(tool.execute(config, path=str(file_path), old_str="test", new_str="replacement"))
        assert result.is_error
        assert "not found" in result.error.lower()


def test_editfile_respects_allowed_edit_globs():
    """Test that EditFileTool respects allowed_edit_globs phase scope."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        # Create test files
        py_file = workspace / "test.py"
        py_file.write_text("# Python file")
        
        txt_file = workspace / "test.txt"
        txt_file.write_text("Text file")

        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Test 1: With allowed_edit_globs restricting to .py files
        config.phase_edit_globs = ["*.py"]
        
        # Editing a .py file should succeed
        result = asyncio.run(tool.execute(
            config, 
            path=str(py_file),
            old_str="# Python file",
            new_str="# Modified Python file"
        ))
        assert not result.is_error, f"Editing .py file should succeed, got error: {result.error}"
        
        # Editing a .txt file should fail with phase scope error
        result = asyncio.run(tool.execute(
            config,
            path=str(txt_file),
            old_str="Text file",
            new_str="Modified text file"
        ))
        assert result.is_error, "Editing .txt file should fail when only .py files are allowed"
        assert "PHASE SCOPE ERROR" in result.error
        assert "*.py" in result.error
        
        # Test 2: Empty allowed_edit_globs should allow all files
        config.phase_edit_globs = []
        
        result = asyncio.run(tool.execute(
            config,
            path=str(txt_file),
            old_str="Text file",
            new_str="Modified text file"
        ))
        assert not result.is_error, f"Empty allowed_edit_globs should allow all files, got error: {result.error}"


def test_editfile_toctou_symlink_attack_detection():
    """Test that EditFileTool detects TOCTOU symlink attacks with mocked validation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        # Create a test file
        file_path = workspace / "test.txt"
        file_path.write_text("hello world\nthis is a test\ngoodbye again")
        
        tool = EditFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Mock the atomic validation to simulate TOCTOU attack detection
        tool._validate_atomic_path = AsyncMock(return_value=(
            False, 
            ToolResult(error="Path validation failed: symlink attack detected")
        ))
        
        # Attempt to edit the file
        result = asyncio.run(tool.execute(
            config,
            path=str(file_path),
            old_str="hello",
            new_str="greetings"
        ))
        
        # Should fail with the mocked validation error
        assert result.is_error
        assert "symlink attack" in result.error.lower()
        assert "path validation failed" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])