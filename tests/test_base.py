"""Tests for the base Tool class functionality."""

import pytest
from unittest.mock import Mock
from pathlib import Path

from harness.tools.base import Tool
from harness.core.config import HarnessConfig


class TestToolCheckPath:
    """Test the _check_path method security validation."""
    
    def test_tool_check_path_rejects_homoglyph(self, tmp_path):
        """Test that _check_path correctly rejects paths containing homoglyphs.
        
        This test directly addresses the falsifiable criterion by verifying
        the integrated security function works.
        """
        # Create a mock tool instance
        class MockTool(Tool):
            name = "mock_tool"
            description = "A mock tool for testing"
            
            def input_schema(self):
                return {"type": "object", "properties": {}}
            
            async def execute(self, config, **params):
                return Mock()
        
        tool = MockTool()
        
        # Create a minimal config with allowed paths
        workspace = str(tmp_path)
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=workspace,
            allowed_paths=[workspace],
        )
        
        # Test with a path containing a Cyrillic 'a' (U+0430) which looks like ASCII 'a'
        malicious_path = f"{workspace}/s\u0430fe/path/file.txt"
        
        # Call _check_path
        result = tool._check_path(config, malicious_path)
        
        # Verify it returns an error
        assert result is not None
        assert result.is_error is True
        # Error message should indicate homoglyph or security violation
        error_lower = result.error.lower()
        assert "homoglyph" in error_lower or "security" in error_lower or "permission" in error_lower
        
        # Test the consolidated method directly
        search_root, allowed, dir_result = tool._check_dir_root(config, malicious_path)
        assert dir_result is not None
        assert dir_result.is_error is True
        assert "homoglyph" in dir_result.error.lower()
    
    def test_tool_check_path_allows_clean_path(self, tmp_path):
        """Test that _check_path allows clean, allowed paths."""
        # Create a mock tool instance
        class MockTool(Tool):
            name = "mock_tool"
            description = "A mock tool for testing"
            
            def input_schema(self):
                return {"type": "object", "properties": {}}
            
            async def execute(self, config, **params):
                return Mock()
        
        tool = MockTool()
        
        # Create a minimal config with allowed paths
        workspace = str(tmp_path)
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=workspace,
            allowed_paths=[workspace],
        )
        
        # Test with a clean path
        clean_path = f"{workspace}/safe/path/file.txt"
        
        # Call _check_path
        result = tool._check_path(config, clean_path)
        
        # Verify it returns None (no error)
        assert result is None
    
    def test_tool_check_path_rejects_outside_allowed(self, tmp_path):
        """Test that _check_path rejects paths outside allowed directories."""
        # Create a mock tool instance
        class MockTool(Tool):
            name = "mock_tool"
            description = "A mock tool for testing"
            
            def input_schema(self):
                return {"type": "object", "properties": {}}
            
            async def execute(self, config, **params):
                return Mock()
        
        tool = MockTool()
        
        # Create a minimal config with allowed paths
        workspace = str(tmp_path)
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=workspace,
            allowed_paths=[workspace],
        )
        
        # Test with a path outside allowed directory
        outside_path = "/outside/path/file.txt"
        
        # Call _check_path
        result = tool._check_path(config, outside_path)
        
        # Verify it returns an error
        assert result is not None
        assert result.is_error is True
        assert "not allowed" in result.error.lower()
    
    def test_tool_check_dir_root_rejects_homoglyph(self, tmp_path):
        """Test that _check_dir_root correctly rejects root paths containing homoglyphs.
        
        This test directly addresses the falsifiable criterion by verifying
        the primary security entry point for tools works correctly.
        """
        # Create a mock tool instance
        class MockTool(Tool):
            name = "mock_tool"
            description = "A mock tool for testing"
            
            def input_schema(self):
                return {"type": "object", "properties": {}}
            
            async def execute(self, config, **params):
                return Mock()
        
        tool = MockTool()
        
        # Create a minimal config with allowed paths
        workspace = str(tmp_path)
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=workspace,
            allowed_paths=[workspace],
        )
        
        # Test with a root path containing a Cyrillic 'a' (U+0430) which looks like ASCII 'a'
        malicious_root = f"{workspace}/s\u0430fe/path"
        
        # Call _check_dir_root
        search_root, allowed, result = tool._check_dir_root(config, malicious_root)
        
        # Verify it returns an error
        assert result is not None
        assert result.is_error is True
        # Error message should indicate homoglyph or security violation
        error_lower = result.error.lower()
        assert "homoglyph" in error_lower or "security" in error_lower or "permission" in error_lower
        
        # Verify the returned paths are safe defaults
        assert str(search_root) == "."
        assert allowed == []