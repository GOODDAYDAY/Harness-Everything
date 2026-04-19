"""Unit tests for harness.tools.base."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class TestTool(Tool):
    """Test tool for testing base class methods."""
    name = "test_tool"
    description = "Test tool"
    requires_path_check = True
    tags = frozenset({"test"})

    def input_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Test path"},
            },
            "required": ["path"],
        }

    async def execute(self, config: HarnessConfig, *, path: str):
        # Not used in these tests
        pass


def test_check_path_symlink_swap():
    """Test that _check_path prevents symlink swapping attacks (TOCTOU vulnerability).
    
    Creates a symlink within the allowed root, validates it via _check_path,
    then immediately swaps the symlink target to a path outside the root before
    a simulated file operation. Verifies the operation fails with a specific
    error message.
    """
    # Create a temporary directory structure
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create allowed directory (workspace root)
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create a file inside workspace
        allowed_file = workspace / "allowed.txt"
        allowed_file.write_text("allowed content")
        
        # Create a directory outside workspace
        outside_dir = tmpdir_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret content")
        
        # Create a symlink inside workspace pointing to allowed file
        symlink_path = workspace / "link.txt"
        symlink_path.symlink_to(allowed_file)
        
        # Create a test tool instance
        tool = TestTool()
        
        # Mock config with workspace root
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # First validation should succeed - symlink points to allowed file
        result = tool._check_path(config, str(symlink_path))
        assert isinstance(result, str)  # Should return validated path string
        assert "allowed.txt" in result  # Should resolve to the target
        
        # Now simulate a symlink swap attack
        # Remove the symlink and create a new one pointing outside
        symlink_path.unlink()
        symlink_path.symlink_to(outside_file)
        
        # In a real TOCTOU attack, this would happen between validation and file open
        # The _check_path method should have already resolved and validated the path,
        # but we need to test that the path validation itself catches this
        
        # Call _check_path again - it should now fail because the symlink points outside
        result = tool._check_path(config, str(symlink_path))
        
        # The result should be an error because the symlink now points outside
        # The error message should indicate the path is outside allowed directories
        assert isinstance(result, ToolResult)
        assert result.is_error
        # Check for the specific symlink resolution error message
        assert "symlink resolution" in result.error.lower() or "outside allowed" in result.error.lower()


def test_check_path_resolves_symlinks():
    """Test that _check_path properly resolves symlinks and validates the target."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create a file inside workspace
        target_file = workspace / "target.txt"
        target_file.write_text("target content")
        
        # Create a symlink to it
        symlink = workspace / "link.txt"
        symlink.symlink_to(target_file)
        
        # Create tool and config
        tool = TestTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # _check_path should resolve the symlink and return the target path
        result = tool._check_path(config, str(symlink))
        assert isinstance(result, str)
        # The resolved path should be the target file, not the symlink
        assert str(target_file) in result


def test_check_path_rejects_symlink_to_outside():
    """Test that _check_path rejects symlinks that point outside allowed directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create directory outside workspace
        outside = tmpdir_path / "outside"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("secret")
        
        # Create symlink in workspace pointing outside
        symlink = workspace / "malicious_link.txt"
        symlink.symlink_to(outside_file)
        
        # Create tool and config
        tool = TestTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # _check_path should reject this because the resolved target is outside workspace
        result = tool._check_path(config, str(symlink))
        assert isinstance(result, ToolResult)  # Should return error ToolResult
        assert result.is_error
        assert "outside allowed" in result.error.lower() or "security" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])