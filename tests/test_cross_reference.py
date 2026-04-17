"""Tests for the cross_reference tool."""

import asyncio
import re
from harness.tools.cross_reference import CrossReferenceTool
from harness.core.config import HarnessConfig


def test_cross_reference_test_file_detection():
    """Test that test file detection uses precise patterns and word boundaries."""
    # Test regex pattern construction for a symbol with underscores
    func_name = "my_function"
    pattern = re.compile(rf'\b{re.escape(func_name)}\b')
    
    # Should match whole word only
    assert pattern.search("my_function()") is not None
    # Note: test_my_function won't match because underscore is a word character
    # and \b doesn't break between test_ and my_function
    assert pattern.search("test my_function") is not None  # Space creates word boundary
    assert pattern.search("my_functionality()") is None  # Substring should not match
    
    # Test with underscore in name (edge case for word boundary)
    func_name_with_underscore = "MyClass.my_method"
    pattern2 = re.compile(rf'\b{re.escape(func_name_with_underscore)}\b')
    # Verify it compiles and can match
    assert pattern2.search("MyClass.my_method") is not None
    
    # Test that pattern doesn't match partial words
    assert pattern.search("my_function") is not None
    # Note: underscores are word characters, so these won't match with \b
    assert pattern.search("test my_function") is not None  # Space creates boundary
    assert pattern.search("my_function test") is not None  # Space creates boundary
    assert pattern.search("my_functionality") is None
    # "not my_function" should match because "my_function" is a whole word
    assert pattern.search("not my_function") is not None
    # But "not_my_function" shouldn't match (underscore connects them)
    assert pattern.search("not_my_function") is None


def test_cross_reference_tool_initialization():
    """Test that the cross_reference tool can be initialized."""
    tool = CrossReferenceTool()
    assert tool.name == "cross_reference"
    # Check description contains key terms
    desc_lower = tool.description.lower()
    assert "symbol" in desc_lower
    assert "defined" in desc_lower
    assert "call sites" in desc_lower
    assert "symbol" in tool.input_schema()["required"]
    
    # Check that the schema has the expected properties
    schema = tool.input_schema()
    assert "symbol" in schema["properties"]
    assert "root" in schema["properties"]
    assert "include_tests" in schema["properties"]


def test_cross_reference_rejects_homoglyph_root(tmp_path):
    """CrossReferenceTool must reject root paths containing homoglyphs."""
    tool = CrossReferenceTool()
    # Use a real temporary directory that exists
    workspace_path = tmp_path / "safe"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = str(workspace_path)
    config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
    # Cyrillic 'a' (U+0430) which visually spoofs ASCII 'a'
    malicious_root = "/s\u0430fe/path"
    
    result = asyncio.run(tool.execute(config, "some_func", root=malicious_root))
    
    assert result.is_error is True
    # Error message should indicate a security/permission violation
    # Specifically check for homoglyph rejection through the base class security layer
    error_lower = result.error.lower()
    # Strengthened assertion: require both "homoglyph" and "permission error"
    assert "homoglyph" in error_lower and "permission error" in error_lower


def test_cross_reference_rejects_invalid_symbol(tmp_path):
    """Test that CrossReferenceTool rejects invalid symbol formats."""
    tool = CrossReferenceTool()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = str(workspace_path)
    config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
    
    # Test invalid symbols that should be rejected
    invalid_symbols = [
        "bad-symbol",      # hyphen not allowed
        "123start",        # starts with digit
        "..traversal",     # path traversal attempt
        "symbol.with..dots",  # multiple consecutive dots
        "symbol/with/slashes",  # path separators
        "symbol\\with\\backslashes",  # Windows path separators
        "",                # empty string
        "   ",             # whitespace only
        "symbol.",         # trailing dot
        ".method",         # leading dot
        "Class..method",   # double dot
        "symbol with spaces",  # spaces
    ]
    
    for invalid_symbol in invalid_symbols:
        result = asyncio.run(tool.execute(config, invalid_symbol, root=""))
        assert result.is_error is True, f"Expected error for symbol: '{invalid_symbol}'"
        assert "Invalid symbol format" in result.error, f"Wrong error message for '{invalid_symbol}': {result.error}"
    
    # Test valid symbols that should NOT return an error
    # (They may not find anything, but shouldn't error on validation)
    valid_symbols = [
        "my_function",
        "_private_func",
        "ClassName",
        "ClassName.method_name",
        "MyClass._private_method",
        "__dunder__",
        "camelCaseMethod",
        "UPPER_CASE_CONSTANT",
    ]
    
    for valid_symbol in valid_symbols:
        result = asyncio.run(tool.execute(config, valid_symbol, root=""))
        # Valid symbols should not error on validation (may error for other reasons like no files found)
        # Check that the error is NOT about invalid symbol format
        if result.is_error:
            assert "Invalid symbol format" not in result.error, f"Valid symbol '{valid_symbol}' incorrectly rejected: {result.error}"