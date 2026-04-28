'''Unit tests for harness.tools.batch_read.'''

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.base import ToolResult
from harness.tools.batch_read import BatchReadTool


def test_batch_read_tool_initialization():
    '''Test that BatchReadTool initializes correctly.'''
    tool = BatchReadTool()
    assert tool.name == 'batch_read'
    assert tool.description is not None
    # Check for key phrases in description
    desc_lower = tool.description.lower()
    assert 'read' in desc_lower
    assert 'file' in desc_lower
    assert 'many' in desc_lower
    
    # Check input schema
    schema = tool.input_schema()
    assert 'type' in schema
    assert schema['type'] == 'object'
    assert 'properties' in schema
    assert 'paths' in schema['properties']
    assert 'limit' in schema['properties']
    assert 'offset' in schema['properties']


def test_batch_read_input_schema_validation():
    '''Schema declares types + defaults; range enforcement lives in execute().'''
    tool = BatchReadTool()
    schema = tool.input_schema()

    paths_prop = schema['properties']['paths']
    assert paths_prop['type'] == 'array'
    assert paths_prop['items']['type'] == 'string'

    limit_prop = schema['properties']['limit']
    assert limit_prop['type'] == 'integer'
    assert limit_prop['default'] == 2000

    offset_prop = schema['properties']['offset']
    assert offset_prop['type'] == 'integer'
    assert offset_prop['default'] == 1


@pytest.mark.asyncio
async def test_batch_read_single_file():
    '''Test reading a single file with batch_read.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file
        test_file = tmpdir_path / 'test.txt'
        test_content = 'line1\nline2\nline3\nline4\nline5'
        test_file.write_text(test_content)
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read
        result = await tool.execute(
            config=config,
            paths=[str(test_file)],
            limit=10,
            offset=1
        )
        
        # Verify result
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert result.output is not None
        
        # Check output format
        output = result.output
        assert str(test_file) in output
        assert 'line1' in output
        assert 'line5' in output
        assert 'lines 1-5 of 5' in output


@pytest.mark.asyncio
async def test_batch_read_multiple_files():
    '''Test reading multiple files with batch_read.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create test files
        files = []
        for i in range(3):
            test_file = tmpdir_path / f'test{i}.txt'
            test_content = f'File {i} line1\nFile {i} line2\nFile {i} line3'
            test_file.write_text(test_content)
            files.append(test_file)
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read
        result = await tool.execute(
            config=config,
            paths=[str(f) for f in files],
            limit=10,
            offset=1
        )
        
        # Verify result
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert result.output is not None
        
        # Check output contains all files
        output = result.output
        for i, test_file in enumerate(files):
            assert str(test_file) in output
            assert f'File {i} line1' in output


@pytest.mark.asyncio
async def test_batch_read_with_limit():
    '''Test reading files with line limit.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file with many lines
        test_file = tmpdir_path / 'test.txt'
        lines = [f'Line {i}' for i in range(1, 21)]  # 20 lines
        test_file.write_text('\n'.join(lines))
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read with limit of 5
        result = await tool.execute(
            config=config,
            paths=[str(test_file)],
            limit=5,
            offset=1
        )
        
        # Verify only 5 lines returned
        output = result.output
        assert 'Line 1' in output
        assert 'Line 5' in output
        assert 'Line 6' not in output
        assert 'lines 1-5 of 20' in output


@pytest.mark.asyncio
async def test_batch_read_with_offset():
    '''Test reading files with offset.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a test file with many lines
        test_file = tmpdir_path / 'test.txt'
        lines = [f'Line {i}' for i in range(1, 21)]  # 20 lines
        test_file.write_text('\n'.join(lines))
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read with offset 10
        result = await tool.execute(
            config=config,
            paths=[str(test_file)],
            limit=10,
            offset=10
        )
        
        # Verify lines start from offset
        output = result.output
        assert 'Line 10' in output
        assert 'Line 19' in output
        assert 'Line 9' not in output
        assert 'Line 20' not in output
        assert 'lines 10-19 of 20' in output


@pytest.mark.asyncio
async def test_batch_read_empty_file():
    '''Test reading an empty file.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create an empty test file
        test_file = tmpdir_path / 'empty.txt'
        test_file.write_text('')
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read
        result = await tool.execute(
            config=config,
            paths=[str(test_file)],
            limit=10,
            offset=1
        )
        
        # Verify empty file result
        output = result.output
        assert str(test_file) in output
        assert 'lines 1-0 of 0' in output or 'lines 1-1 of 0' in output


@pytest.mark.asyncio
async def test_batch_read_nonexistent_file():
    '''Test reading a non-existent file.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read with non-existent file
        result = await tool.execute(
            config=config,
            paths=['nonexistent.txt'],
            limit=10,
            offset=1
        )
        
        # Verify error result
        assert not result.is_error  # Tool succeeded, file read failed
        output = result.output
        assert 'nonexistent.txt' in output
        assert 'ERROR' in output


@pytest.mark.asyncio
async def test_batch_read_mixed_success_and_failure():
    '''Test batch read with some successful and some failed files.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create one valid file
        valid_file = tmpdir_path / 'valid.txt'
        valid_file.write_text('Valid content')
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read with both valid and invalid files
        result = await tool.execute(
            config=config,
            paths=[str(valid_file), 'nonexistent.txt'],
            limit=10,
            offset=1
        )
        
        # Verify mixed results
        assert not result.is_error  # Tool succeeded
        output = result.output
        assert str(valid_file) in output
        assert 'nonexistent.txt' in output
        assert 'ERROR' in output


@pytest.mark.asyncio
async def test_batch_read_max_files_limit():
    '''Test that batch read enforces maximum file limit.'''
    tool = BatchReadTool()
    config = Mock(spec=HarnessConfig)
    config.workspace_root = '/tmp'
    config.allowed_paths = ['/tmp']
    
    # Create 51 paths (exceeds max of 50)
    paths = [f'file{i}.txt' for i in range(51)]
    
    # Execute batch read - should fail validation
    result = await tool.execute(
        config=config,
        paths=paths,
        limit=10,
        offset=1
    )
    
    # Should fail due to validation error
    assert result.is_error is True
    assert result.error is not None
    assert 'cap is' in result.error or 'paths has 51 entries' in result.error


@pytest.mark.asyncio
async def test_batch_read_character_budget():
    '''Test that batch read respects character budget.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create a file with content that would exceed budget if not truncated
        test_file = tmpdir_path / 'test.txt'
        # Create a very long line
        long_line = 'x' * 10000
        test_file.write_text(f'{long_line}\n' * 10)  # 10 lines of 10k chars each = 100k chars
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read
        result = await tool.execute(
            config=config,
            paths=[str(test_file)],
            limit=10,
            offset=1
        )
        
        # Verify result
        assert not result.is_error
        output = result.output
        
        # Check metadata
        assert result.metadata is not None
        assert 'n_ok' in result.metadata
        assert result.metadata['n_ok'] == 1
        assert 'n_err' in result.metadata
        assert result.metadata['n_err'] == 0
        
        # Output should contain the file content (possibly truncated)
        assert str(test_file) in output


@pytest.mark.asyncio
async def test_batch_read_metadata():
    '''Test that batch read returns proper metadata.'''
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        
        # Create test files
        files = []
        for i in range(2):
            test_file = tmpdir_path / f'test{i}.txt'
            test_content = f'File {i} content'
            test_file.write_text(test_content)
            files.append(test_file)
        
        # Create tool and config
        tool = BatchReadTool()
        config = Mock(spec=HarnessConfig)
        config.workspace_root = str(tmpdir_path)
        config.allowed_paths = [str(tmpdir_path)]
        
        # Execute batch read
        result = await tool.execute(
            config=config,
            paths=[str(f) for f in files],
            limit=10,
            offset=1
        )
        
        # Verify metadata
        assert result.metadata is not None
        assert 'n_ok' in result.metadata
        assert 'n_err' in result.metadata
        assert result.metadata['n_ok'] == 2
        assert result.metadata['n_err'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
