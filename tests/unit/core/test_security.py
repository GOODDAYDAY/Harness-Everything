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
        """Test that read_file_atomically prevents TOCTOU symlink swap attacks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            safe_file = tmpdir_path / "data.txt"
            safe_file.write_text("legitimate content")
            malicious_file = tmpdir_path / "malicious.txt"
            malicious_file.write_text("stolen data")

            # Create a symlink pointing to the safe file
            link_path = tmpdir_path / "link"
            link_path.symlink_to(safe_file)

            # First read should succeed
            allowed = [tmpdir_path]
            content = read_file_atomically(link_path, allowed_paths=allowed)
            assert content == "legitimate content"

            # Swap the symlink target to the malicious file
            link_path.unlink()
            link_path.symlink_to(malicious_file)

            # Second read should be blocked by the same allowed_paths check
            # The function should detect the path is no longer within the allowed directory
            # Note: read_file_atomically returns None on failure, doesn't raise PermissionError
            content = read_file_atomically(link_path, allowed_paths=allowed)
            # The function returns None when access is denied
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
        """Test that hardlinks to files outside allowed paths are rejected."""
        with tempfile.TemporaryDirectory() as allowed_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            allowed_path = Path(allowed_tmp)
            outside_path = Path(outside_tmp)

            original = outside_path / "secret.txt"
            original.write_text("confidential")
            hardlink = allowed_path / "link.txt"
            os.link(original, hardlink)

            # Attempt to read via hardlink inside allowed directory
            # Note: read_file_atomically returns None on failure, doesn't raise PermissionError
            content = read_file_atomically(hardlink, allowed_paths=[allowed_path])
            # Should return None because the real file is outside allowed paths
            assert content is None