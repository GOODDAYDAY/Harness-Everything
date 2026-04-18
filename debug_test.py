#!/usr/bin/env python3
import ast
import tempfile
import os
from pathlib import Path
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.tools.cross_reference import CrossReferenceTool
from harness.core.config import HarnessConfig
import asyncio

async def test():
    # Create a temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        
        # Create test file
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
        result = await tool.execute(
            config,
            symbol="MyClass.my_method",
            root=str(workspace),
            include_tests=False
        )
        
        print(f"Result error: {result.is_error}")
        if result.is_error:
            print(f"Error message: {result.error}")
        else:
            import json
            data = json.loads(result.output)
            print(f"Definition: {data['definition']}")
            print(f"Callers count: {len(data['callers'])}")
            for i, caller in enumerate(data['callers']):
                print(f"Caller {i+1}: line {caller['line']}, snippet: {caller['snippet']}")

if __name__ == "__main__":
    asyncio.run(test())