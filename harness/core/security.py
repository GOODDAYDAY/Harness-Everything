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
    """Check if path contains control characters.
    
    Rejects all control characters including whitespace control characters
    that could cause unexpected behavior in path handling.
    
    Args:
        path: The path string to validate
        
    Returns:
        Error message if control character found, None if path is clean
    """
    # Dictionary of control characters with descriptions
    control_chars = {
        '\x00': 'U+0000 (NULL)',
        '\x01': 'U+0001 (SOH)',
        '\x02': 'U+0002 (STX)',
        '\x03': 'U+0003 (ETX)',
        '\x04': 'U+0004 (EOT)',
        '\x05': 'U+0005 (ENQ)',
        '\x06': 'U+0006 (ACK)',
        '\x07': 'U+0007 (BEL)',
        '\x08': 'U+0008 (BS)',
        '\x09': 'U+0009 (TAB)',
        '\x0A': 'U+000A (LF)',
        '\x0B': 'U+000B (VT)',
        '\x0C': 'U+000C (FF)',
        '\x0D': 'U+000D (CR)',
        '\x0E': 'U+000E (SO)',
        '\x0F': 'U+000F (SI)',
        '\x10': 'U+0010 (DLE)',
        '\x11': 'U+0011 (DC1)',
        '\x12': 'U+0012 (DC2)',
        '\x13': 'U+0013 (DC3)',
        '\x14': 'U+0014 (DC4)',
        '\x15': 'U+0015 (NAK)',
        '\x16': 'U+0016 (SYN)',
        '\x17': 'U+0017 (ETB)',
        '\x18': 'U+0018 (CAN)',
        '\x19': 'U+0019 (EM)',
        '\x1A': 'U+001A (SUB)',
        '\x1B': 'U+001B (ESC)',
        '\x1C': 'U+001C (FS)',
        '\x1D': 'U+001D (GS)',
        '\x1E': 'U+001E (RS)',
        '\x1F': 'U+001F (US)',
        '\x7F': 'U+007F (DEL)',
    }
    
    for char, description in control_chars.items():
        if char in path:
            return f"PERMISSION ERROR: Path contains disallowed control character: {description}"
    
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


def _validate_file_within_allowed_paths(file_fd: int, allowed_paths: list[Path]) -> bool:
    """Validate that a file descriptor points to a file within allowed paths.
    
    Implements a three-tier validation strategy:
    1. Linux-specific: Use /proc/self/fd/{file_fd} to resolve the real path
    2. Cross-platform fallback: Compare device/inode with all files under allowed_paths
    3. Deny by default: Return False if validation cannot be performed
    
    Args:
        file_fd: Open file descriptor to validate
        allowed_paths: List of allowed directory paths
        
    Returns:
        True if the file is within allowed_paths, False otherwise
    """
    # Create a hash of allowed_paths for cache key
    allowed_paths_hash = hash(tuple(sorted(str(p) for p in allowed_paths)))
    
    # Get file stats first to check for multiple hardlinks
    try:
        file_stat = os.fstat(file_fd)
        file_dev = file_stat.st_dev
        file_ino = file_stat.st_ino
        
        # SECURITY FIX: Check for multiple hardlinks
        # Files with multiple hardlinks could be accessed from outside allowed paths
        if file_stat.st_nlink > 1:
            # File has multiple hardlinks - potential security risk
            # We need to ensure ALL hardlinks are within allowed paths
            
            # Try to get the path used to open this file
            try:
                proc_path = Path(f"/proc/self/fd/{file_fd}")
                if proc_path.exists():
                    opened_path = proc_path.readlink()
                    opened_path_obj = Path(opened_path)
                    
                    # Check if the opened path is within allowed directories
                    opened_in_allowed = any(
                        opened_path_obj.is_relative_to(allowed) 
                        for allowed in allowed_paths
                    )
                    
                    # If the file has multiple hardlinks, we need to be extra cautious
                    # For security, we'll only allow it if we can verify ALL hardlinks
                    # are within allowed paths. Since we can't easily enumerate all
                    # hardlinks, we'll take a conservative approach and reject
                    # multi-hardlink files when opened via /proc path.
                    if opened_in_allowed:
                        # Even though opened path is in allowed directory,
                        # other hardlinks might exist outside. Reject for safety.
                        return False
            except (OSError, ValueError):
                # If we can't check the opened path, reject multi-hardlink files
                return False
    except OSError:
        # Cannot stat file
        return False
    
    # Tier 1: Linux-specific /proc resolution (fastest and most accurate)
    try:
        # Read the symlink from /proc/self/fd/{file_fd}
        proc_path = Path(f"/proc/self/fd/{file_fd}")
        if proc_path.exists():
            target = proc_path.readlink()
            # Get the real path (resolve any symlinks in the target path)
            real_path = Path(os.path.realpath(str(target)))
            # Check if the real path is within any allowed path
            return any(real_path.is_relative_to(allowed) for allowed in allowed_paths)
    except (OSError, ValueError):
        # /proc not available or other error, fall through to Tier 2
        pass
    
    # Tier 2: Cross-platform device/inode comparison
    try:
        # Get device/inode of the opened file (already have from above)
        # file_dev and file_ino are already set
        
        # Iterate through all files under allowed paths
        for allowed in allowed_paths:
            if not allowed.exists():
                continue
            # Recursively walk through all files
            for root, dirs, files in os.walk(str(allowed)):
                for filename in files:
                    file_path = Path(root) / filename
                    try:
                        stat_result = os.stat(str(file_path))
                        if stat_result.st_dev == file_dev and stat_result.st_ino == file_ino:
                            return True
                    except OSError:
                        # File may have been deleted or permissions changed
                        continue
    except OSError:
        # Cannot stat file or walk directories
        pass
    
    # Tier 3: Deny by default
    return False


def _validate_dir_fd_consistent(dir_fd: int, parent_dir: Path) -> bool:
    """Validate that a directory file descriptor matches the expected path.
    
    Implements device/inode validation to prevent TOCTOU attacks where
    a symlink could be swapped between checking and opening.
    
    Args:
        dir_fd: Open directory file descriptor
        parent_dir: Expected parent directory path
        
    Returns:
        True if the descriptor matches the path, False otherwise
    """
    try:
        # Get device/inode of the opened descriptor
        dir_stat = os.fstat(dir_fd)
        
        # Get device/inode of the expected path
        expected_stat = os.stat(str(parent_dir))
        
        # Compare device and inode
        return (dir_stat.st_dev == expected_stat.st_dev and 
                dir_stat.st_ino == expected_stat.st_ino)
    except OSError:
        return False


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
        
        # 2. Open parent directory FIRST - before any path validation
        # This is critical to eliminate TOCTOU race window
        parent_dir = abs_path.parent
        filename = abs_path.name
        
        # Step 1: Check if parent directory is a symlink before opening
        # This prevents TOCTOU attacks where a symlink could be swapped
        if os.path.islink(str(parent_dir)):
            return "PERMISSION ERROR: Parent directory is a symlink"
        
        try:
            # Try to open directory with secure flags if available
            # O_PATH allows opening symlinks without following them
            dir_flags = getattr(os, 'O_PATH', 0) | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0)
            dir_fd = os.open(str(parent_dir), dir_flags)
        except (OSError, AttributeError):
            # Fallback for systems without O_PATH/O_DIRECTORY
            # Try O_NOFOLLOW to avoid following symlinks
            no_follow = getattr(os, 'O_NOFOLLOW', 0)
            try:
                dir_fd = os.open(str(parent_dir), os.O_RDONLY | no_follow)
            except OSError:
                # If O_NOFOLLOW fails (e.g., on directories), fall back to regular open
                # but we lose TOCTOU protection for symlink swaps
                dir_fd = os.open(str(parent_dir), os.O_RDONLY)
        
        # Step 2: Validate that the opened directory descriptor matches the expected path
        # This implements device/inode validation to prevent TOCTOU attacks
        if not _validate_dir_fd_consistent(dir_fd, parent_dir):
            os.close(dir_fd)
            return "PERMISSION ERROR: Directory descriptor mismatch - possible TOCTOU attack"
        
        # 5. Validate the *real* parent directory is within allowed_paths
        # Get the real path of the parent directory for containment check
        try:
            parent_real = Path(os.path.realpath(str(parent_dir)))
        except OSError:
            return "PERMISSION ERROR: Cannot resolve real path of parent directory"
        
        # Containment check: Ensure the real parent directory is within allowed paths
        if not any(parent_real.is_relative_to(allowed) for allowed in allowed_paths):
            return "PERMISSION ERROR: Parent directory not within allowed paths"
        
        # 5. Open target file relative to dir_fd
        # Use O_NOFOLLOW to prevent following symlinks - this fixes TOCTOU attacks
        # where a symlink could be swapped between opening and validation
        file_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
        try:
            file_fd = os.open(filename, file_flags, dir_fd=dir_fd)
        except OSError:
            return "PERMISSION ERROR: Cannot open file"
        
        # 6. CRITICAL SECURITY FIX: Validate the opened file is within allowed paths
        # This prevents hardlink attacks where a hardlink inside allowed directory
        # points to a file outside allowed directory
        if not _validate_file_within_allowed_paths(file_fd, allowed_paths):
            os.close(file_fd)
            return "PERMISSION ERROR: File not within allowed paths (hardlink attack prevented)"
        
        # 7. Verify the opened file matches the expected file
        file_stat = os.fstat(file_fd)
        try:
            expected_stat = (parent_real / filename).stat()
        except OSError:
            return "PERMISSION ERROR: Cannot stat expected file"
        
        if (file_stat.st_dev != expected_stat.st_dev or 
            file_stat.st_ino != expected_stat.st_ino):
            return "PERMISSION ERROR: File descriptor mismatch"
        
        # 8. Read content
        with os.fdopen(file_fd, 'r', encoding='utf-8', errors='replace') as f:
            file_fd = None
            return f.read()
    except (OSError, PermissionError, UnicodeDecodeError):
        return "PERMISSION ERROR: Error reading file"
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