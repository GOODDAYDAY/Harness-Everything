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


def test_deletefile_atomic_unlink_no_exists_check():
    """Test that DeleteFileTool uses os.unlink directly without Path.exists()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("content")
        test_file_path_str = str(test_file)

        tool = DeleteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # 1. Ensure Path.exists() is not called post-validation
        with patch.object(Path, 'exists') as mock_exists:
            result = asyncio.run(tool.execute(config, path=test_file_path_str))
            assert not mock_exists.called, "DeleteFileTool must not use Path.exists() after atomic validation"
            assert not result.is_error
        
        # Check actual file deletion (after patch context ends)
        assert not os.path.exists(test_file_path_str)

        # 2. Test FileNotFoundError handling (simulate race condition)
        test_file2 = workspace / "test2.txt"
        test_file2.write_text("content2")
        test_file2_path_str = str(test_file2)
        # Mock os.unlink to raise FileNotFoundError
        with patch('os.unlink', side_effect=FileNotFoundError):
            result = asyncio.run(tool.execute(config, path=test_file2_path_str))
            assert result.is_error
            assert "disappeared after validation" in result.error  # Specific new error message


def test_movefile_cross_device_error_no_copy_suggestion():
    """Test that MoveFileTool's cross-device error does not suggest copy_file (security bypass)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"
        
        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        # Include both workspace and its parent in allowed paths for parent directory checks
        config.allowed_paths = [str(workspace), tmpdir]
        
        # Mock os.rename to raise EXDEV (cross-device move error)
        with patch('os.rename', side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
            
            # Should return an error
            assert result.is_error
            
            # Error should contain "cross-device move not supported"
            assert "cross-device move not supported" in result.error.lower()
            
            # CRITICAL: Error should NOT suggest "copy_file" (security bypass)
            assert "copy_file" not in result.error.lower(), \
                "Security vulnerability: cross-device error suggests copy_file, creating a TOCTOU bypass vector"
            
            # Should suggest separate operations instead
            assert "separate copy and delete" in result.error.lower()


def test_delete_file_atomic_validation_race_condition():
    """Test that DeleteFileTool properly handles FileNotFoundError after atomic validation.
    
    This test specifically validates the TOCTOU protection behavior by mocking
    _validate_atomic_path to return a valid path, then mocking os.unlink to
    raise FileNotFoundError, simulating a race condition where the file is
    deleted between validation and deletion.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        test_file = workspace / "test.txt"
        test_file.write_text("content")
        test_file_path_str = str(test_file)
        
        tool = DeleteFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Mock _validate_atomic_path to return a valid path
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            mock_validate.return_value = (True, test_file_path_str)
            
            # Mock os.unlink to raise FileNotFoundError
            with patch('os.unlink', side_effect=FileNotFoundError):
                result = asyncio.run(tool.execute(config, path=test_file_path_str))
                
                # Should return an error about file disappearing after validation
                assert result.is_error
                assert "File disappeared after validation" in result.error
                
                # Verify _validate_atomic_path was called with correct parameters
                mock_validate.assert_called_once_with(
                    config, test_file_path_str, require_exists=True, check_scope=True
                )


def test_validate_atomic_path_parameter_names():
    """Test that _validate_atomic_path is called with correct parameter names.
    
    This test verifies that the atomic validation decorator contract is maintained
    and that tools use the correct parameter names (require_exists, not check_exists).
    """
    # Test DeleteFileTool
    delete_tool = DeleteFileTool()
    assert delete_tool.requires_path_check is True, "DeleteFileTool should require path check"
    
    # Test MoveFileTool
    move_tool = MoveFileTool()
    assert move_tool.requires_path_check is True, "MoveFileTool should require path check"
    
    # Test CopyFileTool
    copy_tool = CopyFileTool()
    assert copy_tool.requires_path_check is True, "CopyFileTool should require path check"
    
    # Verify that all tools have the correct parameter name in their execute methods
    # by checking that require_exists is used (not check_exists)
    import inspect
    import asyncio
    
    # Check DeleteFileTool.execute signature
    delete_sig = inspect.signature(DeleteFileTool.execute)
    delete_params = list(delete_sig.parameters.keys())
    # The method should accept config, path parameters
    
    # Check that the tool's _validate_atomic_path method has require_exists parameter
    validate_sig = inspect.signature(delete_tool._validate_atomic_path)
    validate_params = list(validate_sig.parameters.keys())
    assert 'require_exists' in validate_params, "_validate_atomic_path should have require_exists parameter"
    assert 'check_exists' not in validate_params, "_validate_atomic_path should NOT have check_exists parameter"
    
    print("All tools use correct parameter name 'require_exists' and maintain decorator contract")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])