"""Tests for the symbol_extractor tool."""

import asyncio
import json
from harness.tools.symbol_extractor import SymbolExtractorTool
from harness.core.config import HarnessConfig


def test_symbol_extractor_cross_references_field(tmp_path):
    """Test that symbol_extractor includes cross_references field when requested."""
    # Create workspace directory first so we can put the test file inside it.
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Create a simple Python file *inside* the workspace so path validation passes.
    test_file = workspace / "test_module.py"
    test_file.write_text("""
def my_function():
    '''A test function.'''
    return 42

class MyClass:
    def method(self):
        return "hello"
""")

    # Create config
    config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])
    
    # Create tool instance
    tool = SymbolExtractorTool()
    
    # Test with find_cross_references=True
    result = asyncio.run(tool.execute(
        config,
        path=str(test_file),
        symbols="my_function",
        find_cross_references=True,
        format="json"
    ))
    
    # Should not be an error
    assert not result.is_error, f"Tool returned error: {result.error}"
    
    # Parse JSON output
    parsed = json.loads(result.output)
    
    # Check that cross_references field exists
    assert "cross_references" in parsed, "cross_references field missing from output"
    
    # Check the structure of cross_references
    cross_refs = parsed["cross_references"]
    assert isinstance(cross_refs, dict), "cross_references should be a dictionary"
    
    # Check for required keys
    assert "callers" in cross_refs, "callers key missing from cross_references"
    assert "callees" in cross_refs, "callees key missing from cross_references"
    assert "test_files" in cross_refs, "test_files key missing from cross_references"
    
    # Check that values are lists (empty for now since implementation is not complete)
    assert isinstance(cross_refs["callers"], list), "callers should be a list"
    assert isinstance(cross_refs["callees"], list), "callees should be a list"
    assert isinstance(cross_refs["test_files"], list), "test_files should be a list"
    
    # Test with find_cross_references=False (default)
    result2 = asyncio.run(tool.execute(
        config,
        path=str(test_file),
        symbols="my_function",
        format="json"
    ))
    
    # Should not be an error
    assert not result2.is_error, f"Tool returned error: {result2.error}"
    
    # Parse JSON output
    parsed2 = json.loads(result2.output)
    
    # When find_cross_references is False, cross_references field should not be present
    assert "cross_references" not in parsed2, "cross_references field should not be present when find_cross_references=False"


def test_symbol_extractor_tool_initialization():
    """Test that the symbol_extractor tool can be initialized."""
    tool = SymbolExtractorTool()
    assert tool.name == "symbol_extractor"
    
    # Check description contains key terms
    desc_lower = tool.description.lower()
    assert "symbol" in desc_lower
    assert "extract" in desc_lower
    assert "python" in desc_lower
    
    # Check that the schema has the expected properties
    schema = tool.input_schema()
    assert "path" in schema["properties"]
    assert "symbols" in schema["properties"]
    assert "find_cross_references" in schema["properties"]
    
    # Check that find_cross_references has correct default
    assert schema["properties"]["find_cross_references"]["default"] is False


def test_symbol_extractor_finds_symbols(tmp_path):
    """Test that symbol_extractor can find symbols in a file."""
    # Create workspace directory first so test file lives inside it.
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Create a simple Python file *inside* the workspace.
    test_file = workspace / "test_module.py"
    test_file.write_text("""
def public_function():
    '''Public function.'''
    return 1

def _private_function():
    '''Private function.'''
    return 2

class MyClass:
    def method(self):
        return "method"
    
    @classmethod
    def class_method(cls):
        return "class_method"
""")

    # Create config
    config = HarnessConfig(workspace=str(workspace), allowed_paths=[str(workspace)])
    
    # Create tool instance
    tool = SymbolExtractorTool()
    
    # Test finding a specific function
    result = asyncio.run(tool.execute(
        config,
        path=str(test_file),
        symbols="public_function",
        format="json"
    ))
    
    assert not result.is_error, f"Tool returned error: {result.error}"
    
    parsed = json.loads(result.output)
    assert isinstance(parsed, list), "Output should be a list of symbols"
    assert len(parsed) == 1, "Should find exactly one symbol"
    assert parsed[0]["qualname"] == "public_function"
    assert parsed[0]["kind"] == "function"