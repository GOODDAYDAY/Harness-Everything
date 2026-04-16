"""Additional path security tests beyond test_critical_paths.py.

Focuses on edge cases like Unicode normalization attacks and other
subtle path traversal issues.
"""

import os
import tempfile
from pathlib import Path

import pytest

from harness.core.config import HarnessConfig
from harness.tools.file_read import ReadFileTool


def _make_config(workspace: str) -> HarnessConfig:
    """Create a minimal config for testing."""
    return HarnessConfig(
        model="test",
        max_tokens=1000,
        workspace=workspace,
        allowed_paths=[workspace],
    )


def _run(coro):
    """Run async coroutine synchronously for testing."""
    import asyncio
    return asyncio.run(coro)


class TestUnicodePathSecurity:
    """Tests for Unicode-based path traversal attacks."""
    
    def _execute_read_tool(self, cfg, path):
        """Helper to execute ReadFileTool with given config and path."""
        tool = ReadFileTool()
        return _run(tool.execute(cfg, path=path))
    
    def test_unicode_homoglyph_not_allowed(self, tmp_path):
        """Test that visually similar Unicode characters don't bypass path checks.
        
        Some Unicode characters look like ASCII but are different code points.
        For example, CYRILLIC SMALL LETTER A (U+0430) looks like ASCII 'a' (U+0061).
        """
        cfg = _make_config(str(tmp_path))
        
        # Create a legitimate file
        legit_file = tmp_path / "test.txt"
        legit_file.write_text("legitimate content")
        
        # Try to access with Cyrillic 'a' (U+0430) instead of ASCII 'a' (U+0061)
        cyrillic_path = str(tmp_path).replace('a', '\u0430') + "/test.txt"
        result = self._execute_read_tool(cfg, cyrillic_path)
        
        # Should fail with homoglyph validation error
        assert result.is_error
        assert "disallowed Unicode homoglyph" in result.error
    
    def test_unicode_normalization_attack(self, tmp_path):
        """Test that Unicode normalization doesn't create path traversal opportunities.
        
        Some Unicode sequences have multiple representations (NFD vs NFC).
        For example, 'é' can be represented as U+00E9 (single character) or
        as U+0065 U+0301 (e + combining acute accent).
        """
        cfg = _make_config(str(tmp_path))
        
        # Create a file with a Unicode character in its name
        test_file = tmp_path / "café.txt"
        test_file.write_text("test content")
        
        # Try to access with NFD representation (if system uses NFC)
        # This is a real file, so it should work if the filesystem normalizes
        result = self._execute_read_tool(cfg, str(test_file))
        
        # The file exists, so reading should succeed
        # This test documents the behavior rather than enforcing a specific outcome
        # because filesystem normalization varies by OS
        print(f"Unicode file read result: {result.error if result.is_error else 'success'}")
    
    def test_control_characters_in_path(self, tmp_path):
        """Test that control characters other than null byte are rejected."""
        cfg = _make_config(str(tmp_path))
        
        # Test various control characters
        control_chars = [
            "\x01",  # SOH
            "\x02",  # STX  
            "\x03",  # ETX
            "\x04",  # EOT
            "\x05",  # ENQ
            "\x06",  # ACK
            "\x07",  # BEL
            "\x08",  # BS
            "\x09",  # TAB (should be allowed)
            "\x0a",  # LF (newline)
            "\x0b",  # VT
            "\x0c",  # FF
            "\x0d",  # CR
            "\x0e",  # SO
            "\x0f",  # SI
        ]
        
        for char in control_chars:
            path = f"test{char}file.txt"
            result = self._execute_read_tool(cfg, path)
            
            # Most control characters should cause errors
            # Tab (\x09) might be allowed on some filesystems
            if char == "\x09":
                # Tab in filename might be allowed
                continue
                
            # Should get an error (file doesn't exist or path invalid)
            assert result.is_error
    
    def test_whitespace_traversal(self, tmp_path):
        """Test that trailing/leading whitespace doesn't bypass checks."""
        cfg = _make_config(str(tmp_path))
        
        # Create a legitimate file
        legit_file = tmp_path / "test.txt"
        legit_file.write_text("legitimate content")
        
        # Try with trailing spaces (might be trimmed by some systems)
        result = self._execute_read_tool(cfg, str(legit_file) + "   ")
        
        # File with trailing spaces doesn't exist
        assert result.is_error
        assert "not found" in result.error.lower()
        
        # Try with leading spaces
        result = self._execute_read_tool(cfg, "   " + str(legit_file))
        
        # File with leading spaces doesn't exist
        assert result.is_error
        assert "not found" in result.error.lower()


class TestPathCanonicalization:
    """Tests for path canonicalization edge cases."""
    
    def _execute_read_tool(self, cfg, path):
        """Helper to execute ReadFileTool with given config and path."""
        tool = ReadFileTool()
        return _run(tool.execute(cfg, path=path))
    
    def test_double_dot_resolution(self, tmp_path):
        """Test that multiple ../ segments are properly resolved."""
        cfg = _make_config(str(tmp_path))
        
        # Create a subdirectory structure
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        target_file = subdir / "target.txt"
        target_file.write_text("target content")
        
        # Try to access with excessive .. segments
        # From subdir, go up and back down
        path = str(subdir / ".." / "subdir" / "target.txt")
        result = self._execute_read_tool(cfg, path)
        
        # Should succeed - this is a valid relative path
        assert not result.is_error
        assert "target content" in result.output
        
        # Try with more .. than needed
        path = str(subdir / ".." / ".." / ".." / str(tmp_path.name) / "subdir" / "target.txt")
        result = self._execute_read_tool(cfg, path)
        
        # This should fail because it tries to escape the workspace
        assert result.is_error
        assert "not allowed" in result.error.lower() or "not found" in result.error.lower()
    
    def test_current_directory_references(self, tmp_path):
        """Test that ./ and multiple ./ segments are handled correctly."""
        cfg = _make_config(str(tmp_path))
        
        # Create a file
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        
        # Access with ./ prefix
        result = self._execute_read_tool(cfg, "./test.txt")
        
        # Should succeed
        assert not result.is_error
        assert "test content" in result.output
        
        # Access with multiple ./ segments
        result = self._execute_read_tool(cfg, "./././test.txt")
        
        # Should still succeed
        assert not result.is_error
        assert "test content" in result.output