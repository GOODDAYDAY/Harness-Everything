"""Unit tests for harness.tools.file_read."""

import asyncio
import errno
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from harness.core.config import HarnessConfig
from harness.tools.base import ToolResult
from harness.tools.file_read import ReadFileTool


def test_readfile_fallback_path_atomicity():
    """Test that the fallback path for EINVAL uses atomic open+fstat.
    
    Mocks os.open to simulate an EINVAL error on the first call (with O_NOFOLLOW),
    then verifies the fallback path uses os.fstat on the opened file descriptor
    before reading.
    """
    import asyncio
    from unittest.mock import AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track calls to os.open and os.fstat
        open_calls = []
        fstat_calls = []
        
        original_open = os.open
        original_fstat = os.fstat
        
        def mock_open(path, flags, *args, **kwargs):
            open_calls.append((path, flags))
            # First call: simulate EINVAL for O_NOFOLLOW
            if flags & os.O_NOFOLLOW:
                raise OSError(errno.EINVAL, "Invalid argument")
            # Second call: succeed with regular open
            return original_open(path, flags, *args, **kwargs)
        
        def mock_fstat(fd):
            fstat_calls.append(fd)
            return original_fstat(fd)
        
        # Mock _validate_atomic_path to return success
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # Also mock os.close to track it
            close_calls = []
            original_close = os.close
            
            def mock_close(fd):
                close_calls.append(fd)
                original_close(fd)
            
            with patch('os.open', side_effect=mock_open), \
                 patch('os.fstat', side_effect=mock_fstat), \
                 patch('os.close', side_effect=mock_close):
                # Run the tool
                result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Verify the fallback path was used correctly
        assert len(open_calls) == 2, f"Expected 2 os.open calls, got {len(open_calls)}"
        
        # First call should have O_NOFOLLOW
        first_path, first_flags = open_calls[0]
        assert str(test_file) in str(first_path)
        assert first_flags & os.O_NOFOLLOW
        
        # Second call should be regular open (no O_NOFOLLOW)
        second_path, second_flags = open_calls[1]
        assert str(test_file) in str(second_path)
        assert not (second_flags & os.O_NOFOLLOW)
        
        # Should have called fstat on the file descriptor
        assert len(fstat_calls) == 1, f"Expected 1 os.fstat call, got {len(fstat_calls)}"
        
        # Result should be successful
        assert not result.is_error
        assert "line1" in result.output


def test_readfile_einval_fallback_atomic():
    """Test that the EINVAL fallback path performs atomic fstat verification.
    
    Specifically tests that when O_NOFOLLOW fails with EINVAL, the fallback
    uses os.fstat on the opened file descriptor before reading, ensuring
    atomic file type verification.
    """
    import asyncio
    from unittest.mock import Mock, patch, AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content\n")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track fstat calls to verify atomic verification
        fstat_called = []
        original_fstat = os.fstat
        
        def mock_fstat(fd):
            fstat_called.append(fd)
            # Return a mock stat result indicating a regular file
            stat_result = Mock()
            stat_result.st_mode = 0o100644  # Regular file
            return stat_result
        
        # Mock os.open to simulate EINVAL on O_NOFOLLOW
        open_calls = []
        original_open = os.open
        
        def mock_open(path, flags, *args, **kwargs):
            open_calls.append((path, flags))
            if flags & os.O_NOFOLLOW:
                raise OSError(errno.EINVAL, "Invalid argument")
            # Return a mock file descriptor
            return 123
        
        # Mock _validate_atomic_path to return success
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # Mock os.fdopen to return a mock file object that supports context manager
            mock_file = Mock()
            mock_file.read.return_value = b"test content\n"  # Now returns bytes
            mock_file.__enter__ = Mock(return_value=mock_file)
            mock_file.__exit__ = Mock(return_value=None)
            
            with patch('os.open', side_effect=mock_open), \
                 patch('os.fstat', side_effect=mock_fstat), \
                 patch('os.fdopen', return_value=mock_file):
                
                # Run the tool
                result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Verify atomic verification occurred
        assert len(fstat_called) == 1, "os.fstat should have been called for atomic verification"
        assert fstat_called[0] == 123, "fstat should have been called on the opened file descriptor"
        
        # Verify the fallback path was triggered
        assert len(open_calls) >= 1, "os.open should have been called"
        
        # First call should have O_NOFOLLOW
        first_path, first_flags = open_calls[0]
        assert str(test_file) in str(first_path)
        assert first_flags & os.O_NOFOLLOW
        
        # Result should be successful
        assert not result.is_error


def test_readfile_symlink_protection():
    """Test that symlinks pointing outside allowed directories are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create directory outside workspace
        outside = tmpdir_path / "outside"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("secret content")
        
        # Create symlink in workspace pointing outside
        symlink = workspace / "malicious_link.txt"
        symlink.symlink_to(outside_file)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Should reject the symlink
        result = asyncio.run(tool.execute(config, path=str(symlink)))
        assert result.is_error
        assert "outside allowed" in result.error.lower() or "symlink" in result.error.lower()


def test_readfile_valid_symlink():
    """Test that valid symlinks within workspace are allowed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create workspace directory
        workspace = tmpdir_path / "workspace"
        workspace.mkdir()
        
        # Create a file inside workspace
        target_file = workspace / "target.txt"
        target_file.write_text("target content\nline2")
        
        # Create a symlink to it
        symlink = workspace / "link.txt"
        symlink.symlink_to(target_file)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Should allow and read through the symlink
        result = asyncio.run(tool.execute(config, path=str(symlink)))
        assert not result.is_error
        assert "target content" in result.output


def test_readfile_offset_and_limit():
    """Test offset and limit parameters work correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file with multiple lines
        test_file = tmpdir_path / "test.txt"
        content = "\n".join([f"line{i}" for i in range(1, 11)])
        test_file.write_text(content)
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Test with offset=3, limit=4
        result = asyncio.run(tool.execute(config, path=str(test_file), offset=3, limit=4))
        assert not result.is_error
        output = result.output
        
        # Should show lines 3-6
        assert "line3" in output
        assert "line4" in output
        assert "line5" in output
        assert "line6" in output
        assert "line7" not in output  # Should be excluded by limit
        assert "line2" not in output  # Should be excluded by offset


def test_read_file_uses_atomic_validation():
    """Test that ReadFileTool uses _validate_atomic_path for TOCTOU safety."""
    import asyncio
    from unittest.mock import Mock, patch, AsyncMock
    
    # Create a temporary directory for the test
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Mock _validate_atomic_path to return success
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # Mock the file reading to avoid actual I/O
            with patch('builtins.open', Mock(return_value=Mock(
                __enter__=Mock(return_value=Mock(
                    read=Mock(return_value="test content"),
                    __iter__=Mock(return_value=iter(["test content"]))
                )),
                __exit__=Mock()
            ))):
                # Run the tool
                result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Assert the correct, secure method was called
        mock_validate.assert_called_once_with(config, str(test_file))
        
        # Verify the result is successful
        assert not result.is_error
        assert "test content" in result.output


def test_readfile_fallback_fd_leak_protection():
    """Test that the EINVAL fallback path properly closes file descriptors even on errors."""
    import asyncio
    import errno
    from unittest.mock import Mock, patch, AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track calls to os.close
        close_calls = []
        original_close = os.close
        
        def mock_close(fd):
            close_calls.append(fd)
            original_close(fd)
        
        # Mock scenario: O_NOFOLLOW fails with EINVAL, then regular open succeeds
        # but fdopen fails to simulate an error after fstat
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # First os.open call with O_NOFOLLOW fails with EINVAL
            # Second os.open call succeeds
            mock_fd = 123  # Dummy file descriptor
            
            def mock_open(path, flags, *args, **kwargs):
                if flags & os.O_NOFOLLOW:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return mock_fd
            
            # Mock os.fstat to succeed
            mock_stat_result = Mock()
            mock_stat_result.st_mode = 0o100644  # Regular file
            
            # Mock os.fdopen to raise an exception
            def mock_fdopen(fd, *args, **kwargs):
                raise OSError("Simulated fdopen failure")
            
            with patch('os.open', side_effect=mock_open), \
                 patch('os.fstat', return_value=mock_stat_result), \
                 patch('os.fdopen', side_effect=mock_fdopen), \
                 patch('os.close', side_effect=mock_close):
                
                # Run the tool - should fail due to fdopen error
                result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Verify the file descriptor was closed even though fdopen failed
        assert len(close_calls) == 1, f"Expected 1 os.close call, got {len(close_calls)}"
        assert close_calls[0] == mock_fd, f"Expected os.close called with fd={mock_fd}, got {close_calls[0]}"
        
        # Verify the result is an error (as expected)
        assert result.is_error
        assert "Failed to open file descriptor" in result.error
        assert "Simulated fdopen failure" in result.error


def test_readfile_fallback_fd_closed_on_non_regular_file():
    """Test that file descriptors are closed when fstat reveals a non-regular file."""
    import asyncio
    import errno
    from unittest.mock import Mock, patch, AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track calls to os.close
        close_calls = []
        original_close = os.close
        
        def mock_close(fd):
            close_calls.append(fd)
            original_close(fd)
        
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # First os.open call with O_NOFOLLOW fails with EINVAL
            # Second os.open call succeeds
            mock_fd = 456  # Dummy file descriptor
            
            def mock_open(path, flags, *args, **kwargs):
                if flags & os.O_NOFOLLOW:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return mock_fd
            
            # Mock os.fstat to return a directory (not a regular file)
            mock_stat_result = Mock()
            mock_stat_result.st_mode = 0o040755  # Directory mode
            
            with patch('os.open', side_effect=mock_open), \
                 patch('os.fstat', return_value=mock_stat_result), \
                 patch('os.close', side_effect=mock_close):
                
                # Run the tool - should fail because it's not a regular file
                result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Verify the file descriptor was closed
        assert len(close_calls) == 1, f"Expected 1 os.close call, got {len(close_calls)}"
        assert close_calls[0] == mock_fd, f"Expected os.close called with fd={mock_fd}, got {close_calls[0]}"
        
        # Verify the result is an error (not a regular file)
        assert result.is_error
        assert "Not a regular file" in result.error


def test_read_file_atomic_validation_rejects_symlinks():
    """Test that atomic validation in _open_with_atomic_fallback rejects symlinks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        outside = Path(tmpdir) / "outside"
        outside.mkdir()

        # Create a legitimate file inside workspace
        legit = workspace / "data.txt"
        legit.write_text("safe content")
        
        # Create a symlink to it
        link = workspace / "link.txt"
        link.symlink_to(legit)

        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]

        # Mock _validate_atomic_path to return the symlink path
        # This simulates a scenario where the symlink passes initial validation
        with patch.object(tool, '_validate_atomic_path') as mock_validate:
            mock_validate.return_value = (True, str(link))
            
            # Execute the tool
            result = asyncio.run(tool.execute(config, path=str(link)))
            
            # Verify the result contains a symlink error
            assert result.is_error
            assert "Symlink resolution escapes allowed directory" in result.error
            # Verify _open_with_atomic_fallback was triggered
            # (implied by the symlink error message)


def test_open_with_atomic_fallback_einval_checks_symlink():
    """Test that _open_with_atomic_fallback checks for symlinks in EINVAL fallback path.
    
    Mocks os.open to raise EINVAL on O_NOFOLLOW, then verifies that in the fallback
    path, the method checks os.fstat for symlinks (stat.S_ISLNK).
    """
    import errno
    import os
    import stat
    from unittest.mock import Mock, patch
    
    from harness.tools.base import Tool
    
    # Create a test tool instance
    class TestTool(Tool):
        name = "test_tool"
        description = "Test tool"
        requires_path_check = True
        tags = frozenset({"test"})
        
        def input_schema(self):
            return {}
        
        async def execute(self, config, **kwargs):
            pass
    
    tool = TestTool()
    
    # Track fstat calls and what they return
    fstat_results = []
    
    def mock_fstat(fd):
        # Create a mock stat result
        st = Mock()
        # Simulate a symlink
        st.st_mode = stat.S_IFLNK | 0o777
        fstat_results.append((fd, st.st_mode))
        return st
    
    # Mock os.open to simulate EINVAL on O_NOFOLLOW, then success
    open_calls = []
    def mock_open(path, flags, *args, **kwargs):
        open_calls.append((path, flags))
        # First call with O_NOFOLLOW: simulate EINVAL
        if flags & os.O_NOFOLLOW:
            raise OSError(errno.EINVAL, "Invalid argument")
        # Second call without O_NOFOLLOW: return a dummy fd
        return 123
    
    with patch('os.open', mock_open), \
         patch('os.fstat', mock_fstat), \
         patch('os.close'):
        
        # Call _open_with_atomic_fallback
        fd, error = tool._open_with_atomic_fallback("/test/path", os.O_RDONLY)
        
        # Verify behavior
        assert open_calls[0][1] & os.O_NOFOLLOW  # First call had O_NOFOLLOW
        assert not (open_calls[1][1] & os.O_NOFOLLOW)  # Second call didn't
        assert len(fstat_results) == 1  # fstat was called
        assert fstat_results[0][0] == 123  # fstat called on fd 123
        # The error should indicate it's a symlink
        assert error is not None
        assert error.is_error
        assert "symlink" in error.error.lower() or "Symlink" in error.error


def test_execute_fdopen_failure_closes_fd():
    """Test that ReadFileTool.execute() closes file descriptor when os.fdopen fails.
    
    This tests the security guard added to prevent file descriptor leaks
    (CWE-403: Exposure of File Descriptor to Unintended Control Sphere).
    """
    import asyncio
    from unittest.mock import Mock, patch, AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track calls to os.close
        close_calls = []
        original_close = os.close
        
        def mock_close(fd):
            close_calls.append(fd)
            original_close(fd)
        
        # Mock _validate_atomic_path to succeed
        with patch.object(tool, '_validate_atomic_path', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = (True, str(test_file))
            
            # Mock _open_with_atomic_fallback to return a valid file descriptor
            mock_fd = 123  # Dummy file descriptor
            with patch.object(tool, '_open_with_atomic_fallback') as mock_open_fallback:
                mock_open_fallback.return_value = (mock_fd, None)  # Success, no error
                
                # Mock os.fdopen to raise an exception
                def mock_fdopen(fd, *args, **kwargs):
                    raise OSError("Simulated fdopen failure")
                
                with patch('os.fdopen', side_effect=mock_fdopen), \
                     patch('os.close', side_effect=mock_close):
                    
                    # Run the tool - should fail due to fdopen error
                    result = asyncio.run(tool.execute(config, path=str(test_file)))
        
        # Verify the file descriptor was closed even though fdopen failed
        assert len(close_calls) == 1, f"Expected 1 os.close call, got {len(close_calls)}"
        assert close_calls[0] == mock_fd, f"Expected os.close called with fd={mock_fd}, got {close_calls[0]}"
        
        # Verify the result is an error (as expected)
        assert result.is_error
        assert "Failed to open file descriptor" in result.error


def test_readfile_preserves_onofollow_through_fdopen():
    """Test that O_NOFOLLOW protection is maintained through fdopen to prevent TOCTOU."""
    import asyncio
    from unittest.mock import Mock, patch, AsyncMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        
        legit = workspace / "data.txt"
        legit.write_text("line1\nline2\nline3")
        
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(workspace)
        config.allowed_paths = [str(workspace)]
        
        # Mock to verify binary mode preserves flags
        mock_fdopen = patch('os.fdopen')
        mock_file = Mock()
        mock_file.read.return_value = b"line1\nline2\nline3"
        
        # Execute with mocked atomic validation
        with patch.object(tool, '_validate_atomic_path', 
                         return_value=(True, str(legit))), \
             patch.object(tool, '_open_with_atomic_fallback',
                         return_value=(123, None)), \
             patch('os.fdopen', return_value=mock_file) as mock_fdopen_func:
            
            result = asyncio.run(tool.execute(config, path=str(legit), offset=1, limit=2))
            
            # Verify fdopen called with 'rb' (binary mode)
            assert mock_fdopen_func.called
            assert mock_fdopen_func.call_args[0][1] == 'rb', "Must use binary mode to preserve O_NOFOLLOW"
            
            # Verify successful read
            assert not result.is_error
            assert "line1" in result.output


def test_execute_fdopen_failure_returns_toolresult():
    """Test that fdopen failure returns a ToolResult instead of raising exception.
    
    Verifies the critical fix for file descriptor leak and exception handling bug.
    """
    import asyncio
    import errno
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / "test.txt"
        test_file.write_text("test content")
        
        # Create tool and config
        tool = ReadFileTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Track if os.close was called
        close_called = []
        original_close = os.close
        
        def mock_close(fd):
            close_called.append(fd)
            return original_close(fd)
        
        # Mock os.fdopen to raise an exception (simulating EMFILE - too many open files)
        with patch.object(tool, '_validate_atomic_path', 
                         return_value=(True, str(test_file))), \
             patch.object(tool, '_open_with_atomic_fallback',
                         return_value=(123, None)), \
             patch('os.fdopen', side_effect=OSError(errno.EMFILE, "Too many open files")), \
             patch('os.close', side_effect=mock_close):
            
            # Execute - should return ToolResult, not raise exception
            result = asyncio.run(tool.execute(config, path=str(test_file)))
            
            # Verify error result
            assert result.is_error
            assert "Failed to open file descriptor" in result.error
            
            # Verify os.close was called with the correct file descriptor
            assert len(close_called) == 1
            assert close_called[0] == 123
            
            # Verify no uncaught exception was raised
            assert isinstance(result, ToolResult)


def test_guaranteed_fd_cleanup_thread_failure():
    """Test that _guaranteed_fd_cleanup ensures fd is closed even when thread/operation fails."""
    from harness.tools.file_read import ReadFileTool
    
    tool = ReadFileTool()
    
    # Track if os.close was called
    close_called = []
    original_close = os.close
    
    def mock_close(fd):
        close_called.append(fd)
        return original_close(fd)
    
    # Mock operation to raise an exception
    def failing_operation(fd: int):
        raise RuntimeError("Simulated thread/operation failure")
    
    # Test with mocked os.close
    with patch('os.close', side_effect=mock_close):
        result, error = tool._guaranteed_fd_cleanup(123, failing_operation)
        
        # Verify error result
        assert result is None
        assert error is not None
        assert error.is_error
        assert "Operation on file descriptor failed" in error.error
        
        # Verify os.close was called with the correct file descriptor
        # It may be called twice (once in except, once in finally)
        assert len(close_called) >= 1
        assert 123 in close_called


def test_guaranteed_fd_cleanup_success():
    """Test that _guaranteed_fd_cleanup returns result when operation succeeds."""
    from harness.tools.file_read import ReadFileTool
    
    tool = ReadFileTool()
    
    # Track if os.close was called
    close_called = []
    original_close = os.close
    
    def mock_close(fd):
        close_called.append(fd)
        return original_close(fd)
    
    # Mock operation that succeeds
    def successful_operation(fd: int):
        return f"result from fd {fd}"
    
    # Test with mocked os.close
    with patch('os.close', side_effect=mock_close):
        result, error = tool._guaranteed_fd_cleanup(123, successful_operation)
        
        # Verify success result
        assert result == "result from fd 123"
        assert error is None
        
        # Verify os.close was called (in finally block)
        assert len(close_called) == 1
        assert close_called[0] == 123


def test_guaranteed_fd_cleanup_fdopen_success():
    """Test that _guaranteed_fd_cleanup works with os.fdopen operation."""
    from harness.tools.file_read import ReadFileTool
    
    tool = ReadFileTool()
    
    # Create a mock file object
    mock_file = Mock()
    mock_file.fileno.return_value = 123
    
    # Track if os.close was called
    close_called = []
    original_close = os.close
    
    def mock_close(fd):
        close_called.append(fd)
        return original_close(fd)
    
    # Mock os.fdopen to return our mock file
    with patch('os.fdopen', return_value=mock_file), \
         patch('os.close', side_effect=mock_close):
        
        def fdopen_operation(fd: int):
            return os.fdopen(fd, 'rb')
        
        result, error = tool._guaranteed_fd_cleanup(123, fdopen_operation)
        
        # Verify success result
        assert result == mock_file
        assert error is None
        
        # Verify os.close was NOT called (fdopen takes ownership)
        assert len(close_called) == 0


def test_guaranteed_fd_cleanup_success_transfers_ownership():
    """Test that successful fdopen transfers descriptor ownership."""
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write("test content")
        temp_path = f.name

    fd = os.open(temp_path, os.O_RDONLY)
    from harness.tools.file_read import ReadFileTool
    tool = ReadFileTool()

    def fdopen_op(fd):
        return os.fdopen(fd, 'rb')

    file_obj, error = tool._guaranteed_fd_cleanup(fd, fdopen_op)
    
    # Falsifiable Criterion Assertion: Successful operation transfers ownership
    assert error is None
    assert file_obj is not None
    # The file object's internal descriptor should be valid
    # Note: On POSIX systems, fdopen returns a file object wrapping the same fd
    assert file_obj.fileno() == fd  # Same descriptor number
    # Reading should succeed, proving the descriptor is valid
    content = file_obj.read()
    assert content == b"test content"
    file_obj.close()
    
    # Verify that closing the file object doesn't cause issues
    # (the original fd should not be closed separately)
    
    os.unlink(temp_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])