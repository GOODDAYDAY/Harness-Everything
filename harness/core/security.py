"""Security utilities for path validation and threat mitigation."""

from __future__ import annotations

import os
from pathlib import Path

from harness.core.config import HarnessConfig


def validate_path_no_homoglyphs(path: str, config: HarnessConfig | None = None) -> str | None:
    """Check if path contains Unicode homoglyphs that could bypass security.
    
    Homoglyphs are characters that look like ASCII but are different code points.
    For example, CYRILLIC SMALL LETTER A (U+0430) looks like ASCII 'a' (U+0061).
    
    Args:
        path: The path string to validate
        config: Optional HarnessConfig instance for configurable blocklist.
                If None or config.homoglyph_blocklist is empty, uses minimal set.
        
    Returns:
        Error message if homoglyph found, None if path is clean
    """
    # Use configurable blocklist if available, otherwise minimal high-risk set
    if config and hasattr(config, 'homoglyph_blocklist') and config.homoglyph_blocklist:
        homoglyphs = config.homoglyph_blocklist
    else:
        # Fallback to minimal, high-risk character set
        # These are visual spoofs of ASCII path delimiters or common letters
        homoglyphs = {
            '\u0430': 'Cyrillic small a (looks like ASCII a)',
            '\u04CF': 'Cyrillic small palochka (looks like ASCII l)',
            '\u0500': 'Cyrillic capital komi s (looks like ASCII O)',
            '\u01C3': 'Latin letter retroflex click (looks like ASCII !)',
            '\u0391': 'Greek capital alpha (looks like ASCII A)',
            '\u03B1': 'Greek small alpha (looks like ASCII a)',
            '\u041E': 'Cyrillic capital O (looks like ASCII O)',
            '\u043E': 'Cyrillic small o (looks like ASCII o)',
            '\u0555': 'Armenian comma (looks like ASCII comma)',
            '\u058A': 'Armenian hyphen (looks like ASCII hyphen)',
            '\u2044': 'Fraction slash (looks like ASCII /)',
            '\uFF0F': 'Full-width solidus (looks like ASCII /)',
        }
    
    for char, description in homoglyphs.items():
        if char in path:
            return f"PERMISSION ERROR: Path contains disallowed Unicode homoglyph: {description} (U+{ord(char):04X})"
    
    return None


def validate_path_no_null_bytes(path: str) -> str | None:
    """Check if path contains null bytes which can truncate paths at OS level.
    
    Null bytes in path strings cause undefined behavior on some OSes and can be
    used to truncate the path at the OS level, bypassing prefix checks.
    
    Args:
        path: The path string to validate
        
    Returns:
        Error message if null byte found, None if path is clean
    """
    if "\x00" in path:
        return f"PERMISSION ERROR: path contains null byte: {path!r}"
    return None


def validate_path_no_control_chars(path: str) -> str | None:
    """Check if path contains control characters (except whitespace).
    
    Control characters \x01 through \x1f (except \t, \n, \r) can cause
    unexpected behavior in file systems and path resolution.
    
    Args:
        path: The path string to validate
        
    Returns:
        Error message if control character found, None if path is clean
    """
    for i in range(1, 0x20):
        if i in (0x09, 0x0A, 0x0D):  # \t, \n, \r are allowed
            continue
        if chr(i) in path:
            return f"PERMISSION ERROR: path contains control character U+{i:04X}"
    return None


def validate_path_security(path: str, config: HarnessConfig | None = None) -> str | None:
    """Comprehensive path security validation.
    
    Runs all security checks on a path in the correct order:
    1. Null byte validation (most critical - can truncate paths at OS level)
    2. Control character validation (can cause unexpected behavior)
    3. Unicode homoglyph validation (visual spoofing attacks)
    
    Args:
        path: The path string to validate
        config: Optional HarnessConfig instance for homoglyph blocklist
        
    Returns:
        First error message found, or None if all checks pass
    """
    # Check in security-critical order
    if error := validate_path_no_null_bytes(path):
        return error
    if error := validate_path_no_control_chars(path):
        return error
    if error := validate_path_no_homoglyphs(path, config):
        return error
    return None


def read_file_atomically(path: Path, allowed_paths: list[Path]) -> str | None:
    """Read a file atomically to prevent TOCTOU symlink attacks.
    
    Args:
        path: Path to the file to read.
        allowed_paths: List of allowed directory paths for security containment.
        
    Returns:
        File content as string, or None if the file cannot be read securely.
    """
    fd = None
    try:
        # 1. RESOLVE and VALIDATE path security FIRST
        abs_path = path.resolve()
        if not any(abs_path.is_relative_to(allowed) for allowed in allowed_paths):
            return None

        # 2. Open file descriptor (with O_NOFOLLOW)
        open_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(abs_path), open_flags)

        # 3. FINAL VERIFICATION: Ensure opened fd matches the resolved path
        fd_stat = os.fstat(fd)
        try:
            path_stat = abs_path.stat()
        except OSError:
            return None

        if not (fd_stat.st_dev == path_stat.st_dev and fd_stat.st_ino == path_stat.st_ino):
            return None  # File was swapped after resolution but before open

        # 4. Read content
        with os.fdopen(fd, 'r', encoding='utf-8', errors='replace') as f:
            fd = None
            return f.read()
    except (OSError, PermissionError, UnicodeDecodeError):
        return None
    finally:
        # Only close the file descriptor if os.fdopen didn't take ownership
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass