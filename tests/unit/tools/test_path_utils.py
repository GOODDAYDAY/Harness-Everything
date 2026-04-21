"""Unit tests for harness.tools.path_utils."""

import pytest

from harness.tools.path_utils import extract_written_paths, collect_changed_paths


def test_extract_written_paths_single_path_tools():
    """Test extract_written_paths for single-path tools."""
    # Test write_file
    assert extract_written_paths("write_file", {"path": "/tmp/test.txt"}) == [
        "/tmp/test.txt"
    ]
    assert extract_written_paths("write_file", {"path": "relative/path.txt"}) == [
        "relative/path.txt"
    ]
    assert extract_written_paths("write_file", {"path": None}) == []
    assert extract_written_paths("write_file", {}) == []

    # Test edit_file
    assert extract_written_paths("edit_file", {"path": "/tmp/edit.txt"}) == [
        "/tmp/edit.txt"
    ]

    # Test file_patch
    assert extract_written_paths("file_patch", {"path": "/tmp/patch.txt"}) == [
        "/tmp/patch.txt"
    ]

    # Test find_replace
    assert extract_written_paths("find_replace", {"path": "/tmp/find.txt"}) == [
        "/tmp/find.txt"
    ]

    # Test delete_file
    assert extract_written_paths("delete_file", {"path": "/tmp/delete.txt"}) == [
        "/tmp/delete.txt"
    ]


def test_extract_written_paths_move_copy_tools():
    """Test extract_written_paths for move_file and copy_file (destination only)."""
    # Test move_file
    assert extract_written_paths("move_file", {"destination": "/tmp/dest.txt"}) == [
        "/tmp/dest.txt"
    ]
    assert extract_written_paths("move_file", {"destination": None}) == []
    assert extract_written_paths("move_file", {}) == []

    # Test copy_file
    assert extract_written_paths("copy_file", {"destination": "/tmp/copy.txt"}) == [
        "/tmp/copy.txt"
    ]
    assert extract_written_paths(
        "copy_file", {"source": "/tmp/src.txt", "destination": "/tmp/dest.txt"}
    ) == ["/tmp/dest.txt"]


def test_extract_written_paths_batch_edit():
    """Test extract_written_paths for batch_edit tool."""
    # Test with valid edits
    edits = [
        {"path": "/tmp/file1.txt", "old_str": "old", "new_str": "new"},
        {"path": "/tmp/file2.txt", "old_str": "foo", "new_str": "bar"},
    ]
    assert extract_written_paths("batch_edit", {"edits": edits}) == [
        "/tmp/file1.txt",
        "/tmp/file2.txt",
    ]

    # Test with empty edits list
    assert extract_written_paths("batch_edit", {"edits": []}) == []

    # Test with None edits
    assert extract_written_paths("batch_edit", {"edits": None}) == []

    # Test with missing edits key
    assert extract_written_paths("batch_edit", {}) == []

    # Test with malformed edits (missing path)
    malformed_edits = [
        {"old_str": "old", "new_str": "new"},  # missing path
        {"path": "/tmp/file3.txt", "new_str": "bar"},  # missing old_str is OK
    ]
    assert extract_written_paths("batch_edit", {"edits": malformed_edits}) == [
        "/tmp/file3.txt"
    ]

    # Test with non-dict items in edits list
    mixed_edits = [
        {"path": "/tmp/file4.txt"},
        "not a dict",
        123,
        None,
    ]
    assert extract_written_paths("batch_edit", {"edits": mixed_edits}) == [
        "/tmp/file4.txt"
    ]


def test_extract_written_paths_batch_write():
    """Test extract_written_paths for batch_write tool."""
    # Test with valid files
    files = [
        {"path": "/tmp/file1.txt", "content": "content1"},
        {"path": "/tmp/file2.txt", "content": "content2"},
    ]
    assert extract_written_paths("batch_write", {"files": files}) == [
        "/tmp/file1.txt",
        "/tmp/file2.txt",
    ]

    # Test with empty files list
    assert extract_written_paths("batch_write", {"files": []}) == []

    # Test with None files
    assert extract_written_paths("batch_write", {"files": None}) == []

    # Test with missing files key
    assert extract_written_paths("batch_write", {}) == []

    # Test with malformed files (missing path)
    malformed_files = [
        {"content": "content1"},  # missing path
        {"path": "/tmp/file3.txt", "content": "content3"},
    ]
    assert extract_written_paths("batch_write", {"files": malformed_files}) == [
        "/tmp/file3.txt"
    ]

    # Test with non-dict items in files list
    mixed_files = [
        {"path": "/tmp/file4.txt"},
        "not a dict",
        456,
        None,
    ]
    assert extract_written_paths("batch_write", {"files": mixed_files}) == [
        "/tmp/file4.txt"
    ]


def test_extract_written_paths_unknown_tool():
    """Test extract_written_paths returns empty list for unknown tools."""
    assert extract_written_paths("unknown_tool", {"path": "/tmp/test.txt"}) == []
    assert extract_written_paths("read_file", {"path": "/tmp/test.txt"}) == []
    assert extract_written_paths("batch_read", {"paths": ["/tmp/test.txt"]}) == []
    assert extract_written_paths("", {}) == []
    assert extract_written_paths("tool_with_params", {"key": "value"}) == []


def test_extract_written_paths_path_coercion():
    """Test that extract_written_paths converts path values to strings."""
    # Test with Path objects (common in real usage)
    from pathlib import Path

    path_obj = Path("/tmp/test.txt")
    # str(Path) converts to platform-specific path, so we need to compare the string representation
    result = extract_written_paths("write_file", {"path": path_obj})
    assert result == [str(path_obj)]  # Compare with string representation

    # Test with integer path (edge case)
    assert extract_written_paths("write_file", {"path": 123}) == ["123"]

    # Test with boolean path (edge case)
    assert extract_written_paths("write_file", {"path": True}) == ["True"]


def test_collect_changed_paths_basic():
    """Test collect_changed_paths with basic execution log."""
    execution_log = [
        {"tool": "write_file", "input": {"path": "/tmp/file1.txt"}},
        {"tool": "edit_file", "input": {"path": "/tmp/file2.txt"}},
        {"tool": "delete_file", "input": {"path": "/tmp/file3.txt"}},
    ]

    result = collect_changed_paths(execution_log)
    assert result == ["/tmp/file1.txt", "/tmp/file2.txt", "/tmp/file3.txt"]


def test_collect_changed_paths_deduplication():
    """Test collect_changed_paths deduplicates paths while preserving order."""
    execution_log = [
        {"tool": "write_file", "input": {"path": "/tmp/file1.txt"}},
        {"tool": "edit_file", "input": {"path": "/tmp/file2.txt"}},
        {"tool": "write_file", "input": {"path": "/tmp/file1.txt"}},  # Duplicate
        {
            "tool": "batch_edit",
            "input": {
                "edits": [
                    {"path": "/tmp/file3.txt"},
                    {"path": "/tmp/file2.txt"},  # Duplicate
                ]
            },
        },
    ]

    result = collect_changed_paths(execution_log)
    assert result == ["/tmp/file1.txt", "/tmp/file2.txt", "/tmp/file3.txt"]


def test_collect_changed_paths_success_only():
    """Test collect_changed_paths with success_only=True (default)."""
    execution_log = [
        {"tool": "write_file", "input": {"path": "/tmp/file1.txt"}, "success": True},
        {
            "tool": "edit_file",
            "input": {"path": "/tmp/file2.txt"},
            "success": False,
        },  # Failed
        {"tool": "delete_file", "input": {"path": "/tmp/file3.txt"}, "is_error": False},
        {
            "tool": "write_file",
            "input": {"path": "/tmp/file4.txt"},
            "is_error": True,
        },  # Error
        {
            "tool": "copy_file",
            "input": {"destination": "/tmp/file5.txt"},
        },  # No success/is_error field
    ]

    # With success_only=True (default), should skip failed/errored entries
    result = collect_changed_paths(execution_log, success_only=True)
    assert result == ["/tmp/file1.txt", "/tmp/file3.txt", "/tmp/file5.txt"]

    # With success_only=False, should include all entries
    result = collect_changed_paths(execution_log, success_only=False)
    assert result == [
        "/tmp/file1.txt",
        "/tmp/file2.txt",
        "/tmp/file3.txt",
        "/tmp/file4.txt",
        "/tmp/file5.txt",
    ]


def test_collect_changed_paths_success_field_precedence():
    """Test that 'success' field takes precedence over 'is_error' field."""
    execution_log = [
        # Both fields present, success=True should win
        {
            "tool": "write_file",
            "input": {"path": "/tmp/file1.txt"},
            "success": True,
            "is_error": True,
        },
        # Both fields present, success=False should win
        {
            "tool": "edit_file",
            "input": {"path": "/tmp/file2.txt"},
            "success": False,
            "is_error": False,
        },
        # Only is_error present
        {"tool": "delete_file", "input": {"path": "/tmp/file3.txt"}, "is_error": True},
        {
            "tool": "copy_file",
            "input": {"destination": "/tmp/file4.txt"},
            "is_error": False,
        },
    ]

    result = collect_changed_paths(execution_log, success_only=True)
    assert result == [
        "/tmp/file1.txt",
        "/tmp/file4.txt",
    ]  # file1 (success=True), file4 (is_error=False)


def test_collect_changed_paths_empty_or_missing_fields():
    """Test collect_changed_paths handles missing tool/input fields."""
    execution_log = [
        {},  # Empty entry
        {"tool": "write_file"},  # Missing input
        {"input": {"path": "/tmp/file1.txt"}},  # Missing tool
        {"tool": "", "input": {}},  # Empty tool name
        {"tool": "unknown_tool", "input": {"key": "value"}},  # Unknown tool
    ]

    result = collect_changed_paths(execution_log)
    assert result == []  # No valid paths extracted


def test_collect_changed_paths_mixed_tool_types():
    """Test collect_changed_paths with various tool types."""
    execution_log = [
        {"tool": "write_file", "input": {"path": "/tmp/file1.txt"}},
        {
            "tool": "move_file",
            "input": {"source": "/tmp/src.txt", "destination": "/tmp/dest.txt"},
        },
        {
            "tool": "batch_edit",
            "input": {
                "edits": [
                    {"path": "/tmp/file2.txt"},
                    {"path": "/tmp/file3.txt"},
                ]
            },
        },
        {
            "tool": "batch_write",
            "input": {
                "files": [
                    {"path": "/tmp/file4.txt", "content": "content4"},
                    {"path": "/tmp/file5.txt", "content": "content5"},
                ]
            },
        },
        {"tool": "read_file", "input": {"path": "/tmp/read.txt"}},  # Read-only tool
    ]

    result = collect_changed_paths(execution_log)
    assert result == [
        "/tmp/file1.txt",
        "/tmp/dest.txt",  # Only destination from move_file
        "/tmp/file2.txt",
        "/tmp/file3.txt",
        "/tmp/file4.txt",
        "/tmp/file5.txt",
    ]


def test_collect_changed_paths_empty_log():
    """Test collect_changed_paths with empty execution log."""
    assert collect_changed_paths([]) == []
    assert collect_changed_paths([], success_only=False) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
