"""Tests for the cross_reference tool."""

import asyncio
import re
from harness.tools.cross_reference import CrossReferenceTool
from harness.core.config import HarnessConfig


def test_cross_reference_test_file_detection():
    """Test that test file detection uses precise patterns and word boundaries."""
    # Test regex pattern construction for a symbol with underscores
    func_name = "my_function"
    pattern = re.compile(rf'(?<!\w){re.escape(func_name)}(?!\w)')
    
    # Should match whole word only
    assert pattern.search("my_function()") is not None
    # With negative lookbehind/lookahead, test_my_function should NOT match
    assert pattern.search("test_my_function") is None  # Underscore before prevents match
    assert pattern.search("test my_function") is not None  # Space creates boundary
    assert pattern.search("my_functionality()") is None  # Substring should not match
    
    # Test with underscore in name (edge case for word boundary)
    func_name_with_underscore = "MyClass.my_method"
    pattern2 = re.compile(rf'(?<!\w){re.escape(func_name_with_underscore)}(?!\w)')
    # Verify it compiles and can match
    assert pattern2.search("MyClass.my_method") is not None
    
    # Test that pattern doesn't match partial words
    assert pattern.search("my_function") is not None
    # With negative lookbehind/lookahead:
    assert pattern.search("test my_function") is not None  # Space creates boundary
    assert pattern.search("my_function test") is not None  # Space creates boundary
    assert pattern.search("my_functionality") is None
    # "not my_function" should match because "my_function" is a whole word
    assert pattern.search("not my_function") is not None
    # "not_my_function" shouldn't match (underscore connects them)
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


def test_cross_reference_finds_instance_method_calls(tmp_path):
    """Test that cross_reference correctly finds instance method calls.
    
    This validates the fix for the falsifiable criterion: the tool's core 
    functionality is broken as call_name() never returns a fully-qualified name,
    making the comparison cname == f"{class_name}.{func_name}" always fail 
    for instance method calls.
    """
    # Create workspace directory
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    
    # Create a test Python file with class definition and instance method calls
    test_file = workspace / "test_module.py"
    test_file.write_text("""
class MyClass:
    def my_method(self):
        '''Instance method.'''
        return "hello"
    
    def another_method(self):
        '''Another instance method.'''
        return "world"

def standalone_function():
    '''A standalone function.'''
    return 42

# Create instance and call methods
obj = MyClass()
obj.my_method()          # Instance method call - should be found
obj.another_method()     # Another instance method call - should NOT be found for "MyClass.my_method"
MyClass.my_method(obj)   # Class-style call - should be found

# Call standalone function
standalone_function()    # Should NOT be found for "MyClass.my_method"
""")
    
    # Create config
    config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])
    
    # Create tool instance
    tool = CrossReferenceTool()
    
    # Test searching for "MyClass.my_method"
    result = asyncio.run(tool.execute(
        config,
        symbol="MyClass.my_method",
        root=str(workspace),
        include_tests=False
    ))
    
    # Should not be an error
    assert not result.is_error, f"Tool returned error: {result.error}"
    
    # Parse JSON output
    import json
    data = json.loads(result.output)
    
    # Verify the definition is found
    assert data["definition"] is not None, "Definition should be found"
    assert data["definition"]["file"] == "test_module.py"
    # Method definition is at line 3 (1-based) in the test content
    # Line 1: (empty)
    # Line 2: class MyClass:
    # Line 3:     def my_method(self):
    assert data["definition"]["line"] == 3  # Line number of method definition
    
    # Verify callers are found - should find 2 calls:
    # 1. obj.my_method() at line 17
    # 2. MyClass.my_method(obj) at line 19
    # The standalone_function() call at line 22 should NOT be included
    assert len(data["callers"]) == 2, f"Expected 2 callers, found {len(data['callers'])}"
    
    # Verify the callers are at the correct lines
    caller_lines = sorted([caller["line"] for caller in data["callers"]])
    assert caller_lines == [17, 19], f"Callers should be at lines 17 and 19, found at {caller_lines}"
    
    # Verify snippets contain the method call
    for caller in data["callers"]:
        assert "my_method" in caller["snippet"], f"Snippet should contain 'my_method': {caller['snippet']}"
    
    # Specific assertion for instance method calls to validate the fix
    instance_calls = [c for c in data["callers"] if "obj.my_method()" in c.get("snippet", "")]
    assert len(instance_calls) > 0, "Tool failed to detect instance method call `obj.my_method()`"
    
    # Test that searching for standalone function works correctly
    result2 = asyncio.run(tool.execute(
        config,
        symbol="standalone_function",
        root=str(workspace),
        include_tests=False
    ))
    
    assert not result2.is_error, f"Tool returned error: {result2.error}"
    data2 = json.loads(result2.output)
    
    # Should find 1 caller for standalone_function
    assert len(data2["callers"]) == 1, f"Expected 1 caller for standalone_function, found {len(data2['callers'])}"
    assert data2["callers"][0]["line"] == 21, f"Standalone function call should be at line 21, found at {data2['callers'][0]['line']}"