#!/usr/bin/env python3
"""Quick test of homoglyph validation."""

import asyncio
import tempfile
from pathlib import Path

# Add harness to path
import sys
sys.path.insert(0, '/home/ubuntu/harness-everything')

from harness.core.config import HarnessConfig
from harness.tools.file_read import ReadFileTool

async def test_homoglyph():
    """Test that Cyrillic 'a' is rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = HarnessConfig(
            model="test",
            max_tokens=1000,
            workspace=tmpdir,
            allowed_paths=[tmpdir],
        )
        
        tool = ReadFileTool()
        
        # Create a legitimate file
        legit_file = Path(tmpdir) / "test.txt"
        legit_file.write_text("legitimate content")
        
        # Try to access with Cyrillic 'a' (U+0430) instead of ASCII 'a' (U+0061)
        cyrillic_path = tmpdir.replace('a', '\u0430') + "/test.txt"
        
        print(f"Testing path: {cyrillic_path}")
        cyrillic_char = '\u0430'
        print(f"Contains Cyrillic 'a': {cyrillic_char in cyrillic_path}")
        
        result = await tool.execute(cfg, path=cyrillic_path)
        
        print(f"Result error: {result.error}")
        print(f"Result is_error: {result.is_error}")
        
        if result.is_error and "disallowed Unicode homoglyph" in result.error:
            print("✓ Homoglyph validation works!")
            return True
        else:
            print("✗ Homoglyph validation failed")
            return False

if __name__ == "__main__":
    success = asyncio.run(test_homoglyph())
    sys.exit(0 if success else 1)