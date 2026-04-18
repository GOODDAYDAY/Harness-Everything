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
    dir_fd = None
    file_fd = None
    try:
        # 1. Convert path to absolute (but don't resolve symlinks yet)
        abs_path = path.absolute()
        
        # 2. Open parent directory with O_PATH | O_DIRECTORY | O_CLOEXEC if available
        # This gives us a directory file descriptor that can't be swapped out
        parent_dir = abs_path.parent
        filename = abs_path.name
        
        try:
            # Linux-specific flags for atomic directory access
            dir_flags = getattr(os, 'O_PATH', 0) | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0)
            dir_fd = os.open(str(parent_dir), dir_flags)
        except (OSError, AttributeError):
            # Fallback for systems without O_PATH/O_DIRECTORY
            # Use O_RDONLY and hope the directory doesn't get swapped
            dir_fd = os.open(str(parent_dir), os.O_RDONLY)
        
        # 3. Verify parent directory is within allowed paths using the directory fd
        # This resolves symlinks atomically using the directory file descriptor
        try:
            # Use os.path.realpath with dir_fd to get canonical path
            parent_realpath = os.path.realpath(str(parent_dir), dir_fd=dir_fd)
            parent_realpath_path = Path(parent_realpath)
            if not any(parent_realpath_path.is_relative_to(allowed) for allowed in allowed_paths):
                return None
        except OSError:
            return None
        
        # 4. Verify parent directory is within allowed paths using the directory fd
        try:
            # Use os.path.realpath with dir_fd to get canonical path
            parent_realpath = os.path.realpath(str(parent_dir), dir_fd=dir_fd)
            parent_realpath_path = Path(parent_realpath)
            if not any(parent_realpath_path.is_relative_to(allowed) for allowed in allowed_paths):
                return None
        except OSError:
            return None
        
        # 5. Open the target file relative to the directory fd with O_NOFOLLOW
        # This ensures we open the actual file, not a symlink
        file_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
        try:
            file_fd = os.open(filename, file_flags, dir_fd=dir_fd)
        except OSError:
            return None
        
        # 6. Final verification: ensure the opened file is within allowed paths
        # Use fstat to get device and inode, then verify it matches the intended file
        try:
            # Get file stats for the opened file descriptor
            file_stat = os.fstat(file_fd)
            
            # Construct the resolved file path using the resolved parent directory
            # and the filename we used to open the file
            resolved_file_path = parent_realpath_path / filename
            
            # Get file stats for the resolved path
            try:
                target_stat = resolved_file_path.stat()
            except OSError:
                return None
            
            # Compare device and inode to ensure we opened the intended file
            if (file_stat.st_dev != target_stat.st_dev or 
                file_stat.st_ino != target_stat.st_ino):
                return None
            
            # Also verify the file is within allowed paths using the resolved path
            if not any(resolved_file_path.is_relative_to(allowed) for allowed in allowed_paths):
                return None
        except OSError:
            return None
        
        # 7. Read content
        with os.fdopen(file_fd, 'r', encoding='utf-8', errors='replace') as f:
            file_fd = None
            return f.read()
    except (OSError, PermissionError, UnicodeDecodeError):
        return None
    finally:
        # Clean up file descriptors
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass