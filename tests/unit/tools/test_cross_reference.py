"""Unit tests for the cross_reference tool."""

import asyncio
from pathlib import Path
import re
import pytest
from harness.tools.cross_reference import CrossReferenceTool
from harness.core.config import HarnessConfig


def test_execute_rejects_invalid_symbol_depth(tmp_path):
    """Test that CrossReferenceTool.execute() rejects symbols exceeding maximum depth.
    
    This test validates the falsifiable criterion: the tool must produce a measurable
    security improvement by rejecting deeply nested symbols that could cause DoS attacks.
    """
    # Create a temporary workspace
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = str(workspace_path)
    
    # Create a simple Python file to search
    test_file = workspace_path / "test.py"
    test_file.write_text("""
def some_function():
    pass
""")
    
    # Create config
    config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
    
    # Create the tool
    tool = CrossReferenceTool()
    
    # Test: Symbol exceeding maximum depth (10 dots, 11 identifiers)
    # This should now fail regex validation first (defense-in-depth)
    max_depth_symbol = "a.b.c.d.e.f.g.h.i.j.k"  # 10 dots, 11 identifiers
    
    # First verify regex rejects it (defense-in-depth validation)
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(max_depth_symbol) is None, \
        f"Regex should reject symbol with 11 identifiers: {max_depth_symbol}"
    
    result = asyncio.run(tool.execute(
        config,
        symbol=max_depth_symbol,
        root=workspace
    ))
    
    # The result should be an error because it exceeds _MAX_SYMBOL_IDENTIFIERS
    assert result.is_error, f"Symbol exceeding maximum depth should trigger error: {max_depth_symbol}"
    assert "Symbol validation failed" in result.error, \
        f"Error should mention symbol validation. Got: {result.error}"
    
    # Verify the error contains the exact phrase about exceeding maximum depth
    # The explicit depth check happens first now, so we should get "exceeds maximum depth"
    assert "exceeds maximum depth" in result.error, \
        f"Error should contain 'exceeds maximum depth'. Got: {result.error}"
    assert "a.b.c.d.e.f.g.h.i.j.k" in result.error, \
        f"Error should mention the invalid symbol. Got: {result.error}"
    
    # New, specific assertion for the falsifiable criterion
    assert f"Symbol '{max_depth_symbol}' exceeds maximum depth" in result.error, \
        f"Error message must cite the symbol and mention depth violation for security audit. Got: {result.error}"
    
    # Test: Valid symbol with maximum allowed depth (9 dots, 10 identifiers)
    valid_max_depth_symbol = "a.b.c.d.e.f.g.h.i.j"  # 9 dots, 10 identifiers
    
    # First, test the _validate_symbol_format method directly (boundary condition)
    # This should not raise any exception
    try:
        tool._validate_symbol_format(valid_max_depth_symbol)
    except Exception as e:
        pytest.fail(f"_validate_symbol_format should accept symbol at max depth. Got: {e}")
    
    result = asyncio.run(tool.execute(
        config,
        symbol=valid_max_depth_symbol,
        root=workspace
    ))
    
    # This should not be an error (though it may not find the symbol)
    # We just verify it doesn't fail with validation error
    if result.is_error:
        # If it's an error, it shouldn't be a validation error
        assert "Symbol validation failed" not in result.error, \
            f"Valid symbol at max depth should not trigger validation error. Got: {result.error}"


def test_symbol_validation_regex_alignment():
    """Test that the regex pattern is aligned with the depth limit.
    
    This test validates the falsifiable criterion: the regex pattern must
    reject symbols exceeding the maximum depth, providing defense-in-depth
    security validation.
    """
    tool = CrossReferenceTool()
    
    # Verify the regex pattern contains the correct repetition limit
    pattern_str = tool._VALID_SYMBOL_PATTERN.pattern
    assert "{0,9}" in pattern_str, \
        f"Regex pattern should contain '{{0,9}}' to allow max 10 identifiers (9 dots). Got: {pattern_str}"
    
    # Test that the regex correctly rejects overly deep symbols
    overly_deep_symbol = "a.b.c.d.e.f.g.h.i.j.k"  # 10 dots, 11 identifiers
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(overly_deep_symbol) is None, \
        f"Regex should reject symbol with 11 identifiers: {overly_deep_symbol}"
    
    # Test that the regex correctly accepts symbols at max depth
    valid_max_depth_symbol = "a.b.c.d.e.f.g.h.i.j"  # 9 dots, 10 identifiers
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(valid_max_depth_symbol) is not None, \
        f"Regex should accept symbol with 10 identifiers: {valid_max_depth_symbol}"
    
    # Verify the pattern is ASCII-only (security requirement)
    assert tool._VALID_SYMBOL_PATTERN.flags & re.ASCII, \
        "Regex pattern must be ASCII-only to prevent homoglyph attacks"
    
    # Verify regex rejects empty string (defense-in-depth)
    assert tool._VALID_SYMBOL_PATTERN.fullmatch("") is None, \
        "Regex should reject empty string"


def test_execute_validation_error_integration(tmp_path):
    """Test that execute() properly returns validation errors for invalid symbols.
    
    This test validates the falsifiable criterion by ensuring the tool produces
    measurable error artifacts when given invalid symbols.
    """
    # Create a temporary workspace
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = str(workspace_path)
    
    # Create a simple Python file to search
    test_file = workspace_path / "test.py"
    test_file.write_text("""
def some_function():
    pass
""")
    
    # Create config
    config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
    
    # Create the tool
    tool = CrossReferenceTool()
    
    # Test 1: Empty string symbol
    result = asyncio.run(tool.execute(
        config,
        symbol="",
        root=workspace
    ))
    
    assert result.is_error, "Empty symbol should trigger error"
    assert "Symbol validation failed" in result.error, \
        f"Error should contain 'Symbol validation failed'. Got: {result.error}"
    
    # Test 2: Whitespace-only symbol
    result = asyncio.run(tool.execute(
        config,
        symbol="   ",
        root=workspace
    ))
    
    assert result.is_error, "Whitespace-only symbol should trigger error"
    assert "Symbol validation failed" in result.error, \
        f"Error should contain 'Symbol validation failed'. Got: {result.error}"
    
    # Test 3: Symbol exceeding maximum depth
    overly_deep_symbol = "a.b.c.d.e.f.g.h.i.j.k"  # 11 identifiers
    result = asyncio.run(tool.execute(
        config,
        symbol=overly_deep_symbol,
        root=workspace
    ))
    
    assert result.is_error, f"Symbol exceeding maximum depth should trigger error: {overly_deep_symbol}"
    assert "Symbol validation failed" in result.error, \
        f"Error should contain 'Symbol validation failed'. Got: {result.error}"
    assert "exceeds maximum depth" in result.error, \
        f"Error should contain 'exceeds maximum depth'. Got: {result.error}"
    assert overly_deep_symbol in result.error, \
        f"Error should mention the invalid symbol. Got: {result.error}"
    
    # Test 4: Valid symbol should not trigger validation error
    valid_symbol = "some_function"
    result = asyncio.run(tool.execute(
        config,
        symbol=valid_symbol,
        root=workspace
    ))
    
    # Valid symbol may not be found, but should not be a validation error
    if result.is_error:
        assert "Symbol validation failed" not in result.error, \
            f"Valid symbol should not trigger validation error. Got: {result.error}"


def test_validation_methods_consistent():
    """Test that validation methods produce consistent results.
    
    This test validates the falsifiable criterion by ensuring that
    validate_symbol() and execute() produce congruent validation results,
    preventing security bypasses through inconsistent validation.
    """
    tool = CrossReferenceTool()
    
    test_cases = [
        # (symbol, should_be_valid, description)
        ("simple_function", True, "Simple valid symbol"),
        ("ClassName.method_name", True, "Valid class method"),
        ("a.b.c.d.e.f.g.h.i.j", True, "Valid symbol at max depth (10 identifiers)"),
        ("", False, "Empty symbol"),
        ("   ", False, "Whitespace-only symbol"),
        ("a.b.c.d.e.f.g.h.i.j.k", False, "Symbol exceeding max depth (11 identifiers)"),
        ("bad-symbol", False, "Invalid character in symbol"),
        ("123start", False, "Symbol starting with number"),
        (".leading_dot", False, "Symbol with leading dot"),
        ("trailing_dot.", False, "Symbol with trailing dot"),
        ("double..dots", False, "Symbol with consecutive dots"),
    ]
    
    for symbol, should_be_valid, description in test_cases:
        # Test validate_symbol() method
        validate_symbol_result = None
        try:
            validated = tool.validate_symbol(symbol)
            validate_symbol_result = (True, f"Validated: {validated}")
        except ValueError as e:
            validate_symbol_result = (False, str(e))
        
        # Test execute() method (validation part only)
        # We'll create a minimal config for execute()
        config = HarnessConfig(workspace="/tmp", allowed_paths=["/tmp"])
        execute_result = asyncio.run(tool.execute(config, symbol=symbol, root="/tmp"))
        
        # Check consistency
        if should_be_valid:
            # For valid symbols, validate_symbol should not raise
            assert validate_symbol_result[0] is True, \
                f"{description}: validate_symbol() should accept valid symbol '{symbol}'"
            
            # execute() may not find the symbol, but should not have validation error
            if execute_result.is_error:
                assert "Symbol validation failed" not in execute_result.error, \
                    f"{description}: execute() should not have validation error for valid symbol '{symbol}'. Got: {execute_result.error}"
        else:
            # For invalid symbols, validate_symbol should raise ValueError
            assert validate_symbol_result[0] is False, \
                f"{description}: validate_symbol() should reject invalid symbol '{symbol}'"
            
            # execute() should return validation error
            assert execute_result.is_error, \
                f"{description}: execute() should return error for invalid symbol '{symbol}'"
            assert "Symbol validation failed" in execute_result.error, \
                f"{description}: execute() error should contain 'Symbol validation failed' for invalid symbol '{symbol}'. Got: {execute_result.error}"
            
            # Both should mention the symbol in error (for traceability)
            assert symbol.strip() in validate_symbol_result[1] or symbol.strip() in execute_result.error, \
                f"{description}: Error messages should mention the invalid symbol '{symbol}'. " \
                f"validate_symbol: {validate_symbol_result[1]}, execute: {execute_result.error}"


def test_symbol_depth_boundary_consistency():
    """Test that symbol depth validation is consistent between regex and explicit check.
    
    This test directly addresses the falsifiable criterion by verifying both
    validation paths (_VALID_SYMBOL_PATTERN and _validate_symbol_format) produce
    congruent results for the boundary case, preventing security bypasses.
    """
    import tempfile
    from pathlib import Path
    
    tool = CrossReferenceTool()
    
    # Test 1: Symbol at maximum depth (10 identifiers, 9 dots) should be valid
    max_valid_symbol = "a.b.c.d.e.f.g.h.i.j"  # 10 identifiers, 9 dots
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(max_valid_symbol) is not None, \
        f"Regex should accept symbol at max depth: {max_valid_symbol}"
    
    # validate_symbol should not raise for valid symbol
    validated = tool.validate_symbol(max_valid_symbol)
    assert validated == max_valid_symbol, \
        f"validate_symbol() should return the validated symbol unchanged. Got: {validated}"
    
    # Additional test cases from implementation plan
    # Test regex rejects 11 identifiers (10 dots)
    symbol_11 = "a" + ".a" * 10  # Constructs "a.a.a.a.a.a.a.a.a.a.a"
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(symbol_11) is None, \
        f"Regex should reject symbol with 11 identifiers: {symbol_11}"
    # Test regex accepts 10 identifiers (9 dots) and validation passes
    symbol_10 = "a" + ".a" * 9
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(symbol_10) is not None, \
        f"Regex should accept symbol with 10 identifiers: {symbol_10}"
    is_valid, _ = tool._validate_symbol_format(symbol_10)
    assert is_valid, f"Validation should pass for symbol with 10 identifiers: {symbol_10}"
    
    # Specific assertion for 10-identifier symbol (a.b.c.d.e.f.g.h.i.j) returns (True, "")
    max_valid_symbol = "a.b.c.d.e.f.g.h.i.j"  # 10 identifiers, 9 dots
    is_valid, error_msg = tool._validate_symbol_format(max_valid_symbol)
    assert is_valid, f"_validate_symbol_format should return True for 10-identifier symbol: {max_valid_symbol}"
    assert error_msg == "", f"_validate_symbol_format should return empty error string for valid symbol. Got: {error_msg}"
    
    # Test 2: Symbol exceeding maximum depth (11 identifiers, 10 dots)
    # With regex {0,9}, the pattern should reject 11 identifiers
    too_deep_symbol = "a.b.c.d.e.f.g.h.i.j.k"  # 11 identifiers, 10 dots
    # The regex should reject 11 identifiers (pattern is {0,9})
    assert tool._VALID_SYMBOL_PATTERN.fullmatch(too_deep_symbol) is None, \
        f"Regex with {{0,9}} should reject symbol with 11 identifiers: {too_deep_symbol}"
    
    # validate_symbol should raise ValueError for invalid symbol
    try:
        tool.validate_symbol(too_deep_symbol)
        assert False, f"validate_symbol() should raise ValueError for symbol exceeding max depth: {too_deep_symbol}"
    except ValueError as e:
        error_msg = str(e)
        assert "exceeds maximum depth" in error_msg or "Symbol validation failed" in error_msg, \
            f"Error should mention depth violation. Got: {error_msg}"
        assert too_deep_symbol in error_msg, \
            f"Error should mention the invalid symbol. Got: {error_msg}"
    
    # Test 3: Execute should also reject symbol exceeding max depth
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace = str(workspace_path)
        
        # Create a simple Python file to search
        test_file = workspace_path / "test.py"
        test_file.write_text("""
def some_function():
    pass
""")
        
        # Create config
        config = HarnessConfig(workspace=workspace, allowed_paths=[workspace])
        
        # Execute with symbol exceeding max depth
        result = asyncio.run(tool.execute(
            config,
            symbol=too_deep_symbol,
            root=workspace
        ))
        
        assert result.is_error, f"execute() should return error for symbol exceeding max depth: {too_deep_symbol}"
        assert "exceeds maximum depth" in result.error or "Symbol validation failed" in result.error, \
            f"Error should mention depth violation. Got: {result.error}"
        assert too_deep_symbol in result.error, \
            f"Error should mention the invalid symbol. Got: {result.error}"


def test_validate_symbol_format_whitespace_bypass():
    """Test that whitespace-only symbols are properly rejected.
    
    This test validates the fix for the whitespace validation bypass
    in the _validate_symbol_format method.
    """
    tool = CrossReferenceTool()
    for symbol in ["   ", "\t\t", "\n", " \t\n "]:
        is_valid, error = tool._validate_symbol_format(symbol)
        assert not is_valid
        assert "whitespace-only" in error or "empty" in error