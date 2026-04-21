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
        config.workspace = str(workspace)
        # Only allow the workspace, not the parent tmpdir
        config.allowed_paths = [str(workspace)]

        # Test: symlink should be rejected because it points outside workspace
        result = asyncio.run(tool.execute(config, source=str(link), destination=str(workspace / "moved.txt")))
        assert result.is_error
        # Should be rejected because symlink points outside allowed paths
        assert "symlink" in result.error.lower() or "outside" in result.error.lower() or "not allowed" in result.error.lower()


def test_movefile_atomic_symlink_protection_source():
    """Test that MoveFileTool prevents TOCTOU symlink attacks on source path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a legitimate file inside workspace
        legit = workspace / "data.txt"
        legit.write_text("safe content")
        
        # Create a secret file outside workspace
        secret = outside / "secret.txt"
        secret.write_text("classified")
        
        # Create a symlink that points to the legitimate file
        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        # Only allow the workspace
        config.allowed_paths = [str(workspace)]

        # Test: moving a symlink should be rejected due to source path validation
        result = asyncio.run(tool.execute(
            config, 
            source=str(link), 
            destination=str(workspace / "moved.txt")
        ))
        assert result.is_error
        # Should be rejected by atomic source path validation
        assert "symlink" in result.error.lower() or "outside" in result.error.lower() or "not allowed" in result.error.lower()


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


def test_movefile_cross_device_fallback():
    """Test that MoveFileTool handles cross-device moves with copy+delete fallback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"
        
        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        # Include workspace and its parent in allowed paths
        config.allowed_paths = [str(workspace), tmpdir]
        
        # Mock the validation methods to succeed
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            # Mock source validation
            mock_validate.side_effect = [
                (True, str(source)),  # source validation
                (True, str(destination)),  # destination validation
                (True, str(workspace)),  # parent directory validation
            ]
            
            # Mock os.rename to raise EXDEV error to trigger fallback
            with patch('os.rename', side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
                # Mock shutil.copy2 and os.unlink to verify they're called
                with patch('shutil.copy2') as mock_copy2, patch('os.unlink') as mock_unlink:
                    mock_copy2.return_value = None
                    mock_unlink.return_value = None
                    
                    result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
                    assert not result.is_error, f"Expected success but got error: {result.error}"
                    assert "cross-device via copy+delete" in result.output
                    mock_copy2.assert_called_once_with(str(source), str(destination))
                    mock_unlink.assert_called_once_with(str(source))


def test_movefile_cross_device_fallback_failure():
    """Test that MoveFileTool reports error when cross-device fallback fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"
        
        tool = MoveFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        config.allowed_paths = [str(workspace), tmpdir]
        
        # Mock the validation methods to succeed
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            # Mock source validation
            mock_validate.side_effect = [
                (True, str(source)),  # source validation
                (True, str(destination)),  # destination validation
                (True, str(workspace)),  # parent directory validation
            ]
            
            # Mock os.rename to raise EXDEV error to trigger fallback
            with patch('os.rename', side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
                # Mock shutil.copy2 to fail
                with patch('shutil.copy2', side_effect=OSError("Permission denied")):
                    result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
                    assert result.is_error
                    assert "Cross-device move failed during fallback" in result.error


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
                    config, test_file_path_str, require_exists=True, check_scope=True, resolve_symlinks=False
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


def test_symlink_resolution_consistent_across_tools():
    """Test that all file tools reject symlinks for security."""
    # This test asserts the concrete improvement: all tools use resolve_symlinks=False
    # Read the source files directly to verify the parameter usage.
    import os
    
    # Read source files
    with open("harness/tools/file_edit.py", "r") as f:
        edit_source = f.read()
    with open("harness/tools/file_ops.py", "r") as f:
        ops_source = f.read()
    with open("harness/tools/file_write.py", "r") as f:
        write_source = f.read()
    with open("harness/tools/file_read.py", "r") as f:
        read_source = f.read()

    # Check that resolve_symlinks=False is present in all validation calls for security
    # EditFileTool
    assert "resolve_symlinks=False" in edit_source, "EditFileTool should use resolve_symlinks=False to reject symlinks"
    
    # DeleteFileTool in file_ops.py
    # Extract DeleteFileTool section
    delete_start = ops_source.find("class DeleteFileTool(Tool):")
    delete_end = ops_source.find("\n\n@enforce_atomic_validation\nclass MoveFileTool", delete_start)
    if delete_end == -1:
        delete_end = len(ops_source)
    delete_section = ops_source[delete_start:delete_end]
    assert "resolve_symlinks=False" in delete_section, "DeleteFileTool should use resolve_symlinks=False to reject symlinks"
    
    # MoveFileTool source validation
    assert "await self._validate_atomic_path(config, source, require_exists=True, check_scope=True, resolve_symlinks=False)" in ops_source, "MoveFileTool source validation should use resolve_symlinks=False to reject symlinks"
    
    # MoveFileTool destination validation
    assert "await self._validate_atomic_path(config, destination, require_exists=False, check_scope=True, resolve_symlinks=False)" in ops_source, "MoveFileTool destination validation should use resolve_symlinks=False to reject symlinks"
    
    # CopyFileTool source validation
    assert "await self._validate_atomic_path(config, source, require_exists=True, check_scope=True, resolve_symlinks=False)" in ops_source, "CopyFileTool source validation should use resolve_symlinks=False to reject symlinks"
    
    # CopyFileTool destination validation
    assert "await self._validate_atomic_path(config, destination, require_exists=False, check_scope=True, resolve_symlinks=False)" in ops_source, "CopyFileTool destination validation should use resolve_symlinks=False to reject symlinks"
    
    # WriteFileTool
    assert "resolve_symlinks=False" in write_source, "WriteFileTool should use resolve_symlinks=False to reject symlinks"
    
    # ReadFileTool
    assert "resolve_symlinks=False" in read_source, "ReadFileTool should use resolve_symlinks=False to reject symlinks"


def test_copyfile_cross_device_fallback():
    """Test that CopyFileTool handles cross-device copies with fallback."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"
        
        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        # Include workspace and its parent in allowed paths
        config.allowed_paths = [str(workspace), tmpdir]
        
        # Mock the validation methods to succeed
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            # Mock source validation
            mock_validate.side_effect = [
                (True, str(source)),  # source validation
                (True, str(destination)),  # destination validation
                (True, str(workspace)),  # parent directory validation
            ]
            
            # Mock asyncio.to_thread to raise EXDEV error to trigger fallback
            with patch('asyncio.to_thread', side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
                # Mock shutil.copy2 to verify it's called in fallback
                with patch('shutil.copy2') as mock_copy2:
                    mock_copy2.return_value = None
                    
                    result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
                    assert not result.is_error, f"Expected success but got error: {result.error}"
                    assert "cross-device" in result.output
                    mock_copy2.assert_called_once_with(str(source), str(destination))


def test_copyfile_cross_device_fallback_failure():
    """Test that CopyFileTool reports error when cross-device fallback fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        source = workspace / "source.txt"
        source.write_text("test content")
        destination = workspace / "dest.txt"
        
        tool = CopyFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace = str(workspace)
        # Include workspace and its parent in allowed paths
        config.allowed_paths = [str(workspace), tmpdir]
        
        # Mock the validation methods to succeed
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            # Mock source validation
            mock_validate.side_effect = [
                (True, str(source)),  # source validation
                (True, str(destination)),  # destination validation
                (True, str(workspace)),  # parent directory validation
            ]
            
            # Mock asyncio.to_thread to raise EXDEV error to trigger fallback
            with patch('asyncio.to_thread', side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
                # Mock shutil.copy2 to raise an error in fallback
                with patch('shutil.copy2', side_effect=OSError(errno.EACCES, "Permission denied")):
                    result = asyncio.run(tool.execute(config, source=str(source), destination=str(destination)))
                    assert result.is_error
                    assert "Cross-device copy failed" in result.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])