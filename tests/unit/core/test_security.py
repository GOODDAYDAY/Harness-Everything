"""Unit tests for harness.core.security functions.

These tests specifically target the security validation functions and
read_file_atomically to address critical gaps in test coverage identified
by evaluators.
"""

import os
import tempfile
from pathlib import Path
import pytest
from harness.core.security import (
    validate_path_no_homoglyphs,
    validate_path_no_null_bytes,
    validate_path_no_control_chars,
    validate_path_security,
    read_file_atomically,
)
from harness.core.config import HarnessConfig


class TestSecurity:
    def test_validate_path_no_homoglyphs_with_config(self):
        """Test homoglyph detection with a custom blocklist from HarnessConfig."""
        config = HarnessConfig(homoglyph_blocklist={'\u0430': 'Cyrillic a'})
        # Test passes with clean ASCII
        assert validate_path_no_homoglyphs("/safe/path", config) is None
        # Test detects configured homoglyph
        result = validate_path_no_homoglyphs("/uns\u0430fe/path", config)
        assert result is not None
        assert "PERMISSION ERROR" in result
        assert "Cyrillic a" in result

    def test_read_file_atomically_symlink_attack(self):
        """Test that read_file_atomically prevents TOCTOU symlink swap attacks.
        
        The security fix validates symlinks properly to prevent TOCTOU attacks.
        After a symlink swap, the second read must fail to prevent TOCTOU attacks.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            safe_file = tmpdir_path / "data.txt"
            safe_file.write_text("legitimate content")
            malicious_file = tmpdir_path / "malicious.txt"
            malicious_file.write_text("stolen data")

            # Create a symlink pointing to the safe file
            link_path = tmpdir_path / "link"
            link_path.symlink_to(safe_file)

            # First read should succeed (symlink to allowed file)
            allowed = [tmpdir_path]
            content = read_file_atomically(link_path, allowed_paths=allowed)
            assert content == "legitimate content"

            # Swap the symlink target to the malicious file
            link_path.unlink()
            link_path.symlink_to(malicious_file)

            # Second read: After symlink swap, TOCTOU protection should detect
            # the change and return None to prevent the attack
            content = read_file_atomically(link_path, allowed_paths=allowed)
            assert content is None

    def test_validate_path_security_order(self):
        """Validate that checks execute in security-critical order: null bytes first."""
        # A path with both a null byte and a homoglyph should trigger the null byte error first
        test_path = "safe/\x00\u0430path"
        error = validate_path_security(test_path)
        assert error is not None
        # The error should be about the null byte, not the homoglyph
        assert "null byte" in error
        assert "homoglyph" not in error

    def test_read_file_atomically_hardlink_scenario(self):
        """Test that hardlinks to files outside allowed paths are rejected.
        
        This test verifies that the security fix for hardlink attacks works correctly.
        The hardlink validation now checks the actual file location, not just the
        hardlink path, preventing access to files outside allowed directories.
        """
        with tempfile.TemporaryDirectory() as allowed_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            allowed_path = Path(allowed_tmp)
            outside_path = Path(outside_tmp)

            original = outside_path / "secret.txt"
            original.write_text("confidential")
            hardlink = allowed_path / "link.txt"
            os.link(original, hardlink)

            # Attempt to read via hardlink inside allowed directory
            # The security fix should reject this because the actual file
            # is outside the allowed directory
            content = read_file_atomically(hardlink, allowed_paths=[allowed_path])
            # The security fix now correctly rejects hardlinks to files outside allowed paths
            # Function returns None on security failures
            assert content is None

    def test_read_file_atomically_toctou_dir_fd_validation(self):
        """Test that TOCTOU attack via parent directory symlink swap is prevented."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            allowed = tmpdir_path / "allowed"
            allowed.mkdir()
            disallowed = tmpdir_path / "disallowed"
            disallowed.mkdir()

            # Create a file inside the allowed directory
            safe_file = allowed / "data.txt"
            safe_file.write_text("safe")

            # Create a symlink for the *parent directory*
            link_to_allowed = tmpdir_path / "link_to_dir"
            link_to_allowed.symlink_to(allowed)

            # Path via the directory symlink
            file_via_link = link_to_allowed / "data.txt"

            # First read should succeed
            content = read_file_atomically(file_via_link, allowed_paths=[allowed])
            assert content == "safe"

            # Attack: Swap the directory symlink to point to disallowed
            link_to_allowed.unlink()
            link_to_allowed.symlink_to(disallowed)
            # Create a file with the same name in the disallowed directory
            malicious_file = disallowed / "data.txt"
            malicious_file.write_text("malicious")

            # Second read must fail. The opened dir_fd (via link_to_allowed) will
            # point to 'disallowed'. The validation step will check if the real
            # path of 'disallowed' is within allowed_paths ([allowed]), which it is not.
            content = read_file_atomically(file_via_link, allowed_paths=[allowed])
            # The fix ensures this returns None on permission error
            assert content is None

    def test_validate_path_no_null_bytes(self):
        """Test that null bytes in paths are properly rejected."""
        # Assert that clean path returns None
        assert validate_path_no_null_bytes("/safe/path") is None
        
        # Assert that path with null byte returns an error
        error = validate_path_no_null_bytes("bad/\x00path")
        assert error is not None
        assert "null byte" in error
        assert "PERMISSION ERROR" in error
        
        # Test null byte at different positions
        error = validate_path_no_null_bytes("\x00start")
        assert error is not None
        assert "null byte" in error
        
        error = validate_path_no_null_bytes("end\x00")
        assert error is not None
        assert "null byte" in error
        
        error = validate_path_no_null_bytes("middle\x00null")
        assert error is not None
        assert "null byte" in error

    def test_validate_path_no_control_chars_del(self):
        """Test that DEL character (U+007F) and whitespace control characters are properly rejected."""
        # Assert that path with DEL character returns an error
        error = validate_path_no_control_chars('safe/path\x7ftest')
        assert error is not None
        assert "U+007F (DEL)" in error
        
        # Assert that clean path returns None
        assert validate_path_no_control_chars('safe/path') is None
        
        # Assert that whitespace control characters are now rejected
        error = validate_path_no_control_chars('safe/\tpath')
        assert error is not None
        assert "U+0009 (TAB)" in error
        
        error = validate_path_no_control_chars('safe/\npath')
        assert error is not None
        assert "U+000A (LF)" in error
        
        error = validate_path_no_control_chars('safe/\rpath')
        assert error is not None
        assert "U+000D (CR)" in error
        
        # Test other whitespace control characters
        error = validate_path_no_control_chars('safe/\x0Bpath')
        assert error is not None
        assert "U+000B (VT)" in error
        
        error = validate_path_no_control_chars('safe/\x0Cpath')
        assert error is not None
        assert "U+000C (FF)" in error

    def test_read_file_atomically_file_not_found(self):
        """Test that read_file_atomically returns None for non-existent files.
        
        This tests error-handling behavior when a file doesn't exist, which is
        a critical path for security functions.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            # Create a path to a non-existent file
            nonexistent_path = tmpdir_path / "does_not_exist.txt"
            
            # Attempt to read non-existent file
            content = read_file_atomically(nonexistent_path, allowed_paths=[tmpdir_path])
            
            # Should return None for non-existent files
            assert content is None