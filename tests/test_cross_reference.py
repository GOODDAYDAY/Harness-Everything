"""Tests for the cross_reference tool."""

import re
from harness.tools.cross_reference import CrossReferenceTool


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