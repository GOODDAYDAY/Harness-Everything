"""Security utilities for path validation and threat mitigation."""

from __future__ import annotations

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