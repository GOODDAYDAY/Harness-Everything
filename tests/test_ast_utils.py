"""Tests for AST utilities in harness/tools/_ast_utils.py."""

import ast
import sys
from harness.tools._ast_utils import safe_parse, find_symbol_references


def test_safe_parse_exception_handling():
    """Test that safe_parse returns None for various error cases."""
    # Test SyntaxError handling
    invalid_syntax = "def invalid: pass"
    result = safe_parse(invalid_syntax)
    assert result is None, "safe_parse should return None for SyntaxError"
    
    # Test MemoryError simulation with very large input
    # Create a string that's extremely large to potentially cause MemoryError
    # Note: We're not actually guaranteeing MemoryError, but testing the exception handling
    huge_string = "x" * (10**7)  # 10 million characters
    if sys.getsizeof(huge_string) < 2**31:  # Check if we can allocate it
        result = safe_parse(huge_string)
        # safe_parse might return None or raise MemoryError, both are handled
    
    # Test RecursionError with deeply nested structure
    # Create a deeply nested expression that could cause RecursionError
    # Using moderate nesting to avoid platform-dependent issues
    deeply_nested = "[" * 500 + "]" * 500
    result = safe_parse(deeply_nested)
    # safe_parse should handle this without crashing
    
    # Test valid Python code
    valid_code = "def foo():\n    return 42"
    result = safe_parse(valid_code)
    assert isinstance(result, ast.Module), "safe_parse should return ast.Module for valid code"
    assert len(result.body) == 1
    assert isinstance(result.body[0], ast.FunctionDef)
    assert result.body[0].name == "foo"


def test_safe_parse_filename():
    """Test that safe_parse accepts and uses filename parameter."""
    result = safe_parse("x = 1", filename="test.py")
    assert isinstance(result, ast.Module)
    
    # Test with invalid syntax and filename
    result = safe_parse("invalid syntax here", filename="bad.py")
    assert result is None


def test_find_symbol_references_finds_method_call():
    """Test that find_symbol_references correctly locates a class method call."""
    # Sample code with a class method definition and call
    sample_code = """class TestClass:
    def method(self):
        pass

obj = TestClass()
obj.method()
"""
    
    # Parse the code
    tree = ast.parse(sample_code)
    
    # Look for method references (just "method", not "TestClass.method")
    # Since obj.method() is the call, not TestClass.method()
    result = find_symbol_references(tree, "method", "test.py")
    
    # Should find the method call
    assert len(result["calls"]) == 1, f"Expected 1 call, found {len(result['calls'])}"
    
    # Should find the method definition
    assert len(result["definitions"]) == 1, f"Expected 1 definition, found {len(result['definitions'])}"
    
    # Verify the call is at the right line (line 6)
    call_line, call_col = result["calls"][0]
    assert call_line == 6, f"Call should be at line 6, found at line {call_line}"
    
    # Verify the definition is at the right line (line 2)
    def_line, def_col = result["definitions"][0]
    assert def_line == 2, f"Definition should be at line 2, found at line {def_line}"
    
    # Test looking for the class definition
    result2 = find_symbol_references(tree, "TestClass", "test.py")
    # Should find the class definition and the constructor call
    assert len(result2["definitions"]) == 1, f"Expected 1 class definition, found {len(result2['definitions'])}"
    assert len(result2["calls"]) == 1, f"Expected 1 constructor call, found {len(result2['calls'])}"