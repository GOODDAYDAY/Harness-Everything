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
    
    # Tier 2: Cross-platform device/inode comparison with caching
    try:
        # Get device/inode of the opened file
        file_stat = os.fstat(file_fd)
        file_dev = file_stat.st_dev
        file_ino = file_stat.st_ino
        
        # Check cache first
        cached_result = _validate_file_within_allowed_paths_cached(file_dev, file_ino, allowed_paths_hash)
        if cached_result:
            return True
        
        # Cache miss - perform full search
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
                            # Update cache with positive result
                            # Note: We can't update the LRU cache from here because
                            # it's read-only in this context. The cache is populated
                            # on cache misses in subsequent calls.
                            return True
                    except OSError:
                        # File may have been deleted or permissions changed
                        continue
    except OSError:
        # Cannot stat file or walk directories
        pass
    
    # Tier 3: Deny by default
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
        
        # 3. Get device/inode of the opened directory descriptor
        dir_stat = os.fstat(dir_fd)
        
        # 4. Check if what we opened matches what we expected
        # Get stats of the original path (without following symlinks)
        try:
            original_stat = os.lstat(str(parent_dir))
        except OSError:
            return None
        
        # CRITICAL FIX: Always verify the opened directory descriptor matches the original path
        # This prevents TOCTOU attacks where a symlink is swapped after we open it
        if not (dir_stat.st_dev == original_stat.st_dev and dir_stat.st_ino == original_stat.st_ino):
            # What we opened doesn't match the original path
            # This could mean:
            # 1. The path was a symlink and we opened its target (O_RDONLY fallback)
            # 2. The symlink was swapped after we opened it
            # 3. A race condition occurred
            # In all cases, we must fail securely
            
            # Special case: If the original path is a symlink and we opened its target,
            # we need to check if the symlink still points to what we opened
            if os.path.islink(str(parent_dir)):
                try:
                    # Get current symlink target
                    current_target = Path(os.readlink(str(parent_dir)))
                    if not current_target.is_absolute():
                        current_target = (parent_dir.parent / current_target).resolve()
                    
                    # Get stats of current target
                    target_stat = current_target.stat()
                    
                    # Check if what we opened matches the current target
                    if not (dir_stat.st_dev == target_stat.st_dev and dir_stat.st_ino == target_stat.st_ino):
                        return None  # Symlink points elsewhere now
                except OSError:
                    return None
            else:
                # Not a symlink - this is definitely a race condition or attack
                return None
        
        # 5. Validate the *real* parent directory is within allowed_paths
        # Get the real path of the parent directory for containment check
        try:
            parent_real = Path(os.path.realpath(str(parent_dir)))
        except OSError:
            return None
        
        # Containment check: Ensure the real parent directory is within allowed paths
        if not any(parent_real.is_relative_to(allowed) for allowed in allowed_paths):
            return None
        
        # 5. Open target file relative to dir_fd
        # Note: We don't use O_NOFOLLOW because we need to allow symlinks,
        # but we validate the opened file matches the expected file
        file_flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
        try:
            file_fd = os.open(filename, file_flags, dir_fd=dir_fd)
        except OSError:
            return None
        
        # 6. CRITICAL SECURITY FIX: Validate the opened file is within allowed paths
        # This prevents hardlink attacks where a hardlink inside allowed directory
        # points to a file outside allowed directory
        if not _validate_file_within_allowed_paths(file_fd, allowed_paths):
            os.close(file_fd)
            return None
        
        # 7. Verify the opened file matches the expected file
        file_stat = os.fstat(file_fd)
        try:
            expected_stat = (parent_real / filename).stat()
        except OSError:
            return None
        
        if (file_stat.st_dev != expected_stat.st_dev or 
            file_stat.st_ino != expected_stat.st_ino):
            return None
        
        # 8. Read content
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