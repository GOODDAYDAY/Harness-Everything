"""Unit tests for harness.core.llm._summarise_tool_input.

This function produces a short human-readable summary of a tool call's
key parameters, used in log lines. It handles path-based tools, bash,
search tools, and generic fallback.
"""

from harness.core.llm import _summarise_tool_input


# ---------------------------------------------------------------------------
# File-path tools: path / source / destination
# ---------------------------------------------------------------------------

class TestFilePathTools:
    """Tools with 'path', 'source', or 'destination' keys."""

    def test_path_key_returned_as_parenthesised_string(self) -> None:
        result = _summarise_tool_input("edit_file", {"path": "foo.py", "old_str": "x", "new_str": "y"})
        assert result == "(foo.py)"

    def test_source_key_used_when_path_absent(self) -> None:
        result = _summarise_tool_input("move_file", {"source": "a.py", "destination": "b.py"})
        assert result == "(a.py)"

    def test_destination_key_used_when_path_and_source_absent(self) -> None:
        result = _summarise_tool_input("copy_file", {"destination": "b.py"})
        assert result == "(b.py)"

    def test_path_takes_priority_over_source(self) -> None:
        # Path-based lookup checks 'path' first, then 'source', then 'destination'
        result = _summarise_tool_input(
            "some_tool", {"path": "/a", "source": "/b", "destination": "/c"}
        )
        assert result == "(/a)"

    def test_long_path_truncated_with_ellipsis_prefix(self) -> None:
        long_path = "a/" * 50 + "file.py"  # > 80 chars
        result = _summarise_tool_input("read_file", {"path": long_path})
        # Should start with '(' and '\u2026' ellipsis, end with ')'
        assert result.startswith("(…")
        assert result.endswith(")")
        # The total display is at most 80 chars inside the parens
        inner = result[1:-1]  # remove outer parens
        assert len(inner) <= 80

    def test_exactly_80_char_path_not_truncated(self) -> None:
        path = "x" * 80
        result = _summarise_tool_input("read_file", {"path": path})
        assert result == f"({path})"

    def test_81_char_path_is_truncated(self) -> None:
        path = "x" * 81
        result = _summarise_tool_input("read_file", {"path": path})
        assert "\u2026" in result
        assert result.startswith("(…")

    def test_delete_file_path(self) -> None:
        result = _summarise_tool_input("delete_file", {"path": "to_delete.py"})
        assert result == "(to_delete.py)"

    def test_create_directory_path(self) -> None:
        result = _summarise_tool_input("create_directory", {"path": "/some/dir"})
        assert result == "(/some/dir)"

    def test_list_directory_path(self) -> None:
        result = _summarise_tool_input("list_directory", {"path": "harness/"})
        assert result == "(harness/)"

    def test_empty_path_falls_through_to_fallback(self) -> None:
        # Empty string is falsy — path handler skips it, falls through to fallback
        # Fallback shows the first key=value pair from params iteration order
        result = _summarise_tool_input("edit_file", {"path": "", "new_str": "x"})
        # The first key iterated is 'path' with value '', shown via fallback
        assert "path" in result


# ---------------------------------------------------------------------------
# Bash tool
# ---------------------------------------------------------------------------

class TestBashTool:
    """Special handling for bash commands."""

    def test_bash_command_shown_with_dollar_prefix(self) -> None:
        result = _summarise_tool_input("bash", {"command": "pytest tests/ -q"})
        assert result == "($ pytest tests/ -q)"

    def test_bash_empty_command(self) -> None:
        result = _summarise_tool_input("bash", {"command": ""})
        assert result == "($ )"

    def test_bash_command_exactly_80_chars_not_truncated(self) -> None:
        cmd = "echo " + "x" * 75  # 5 + 75 = 80
        result = _summarise_tool_input("bash", {"command": cmd})
        assert result == f"($ {cmd})"

    def test_bash_command_over_80_chars_truncated_with_ellipsis_suffix(self) -> None:
        cmd = "echo " + "x" * 100
        result = _summarise_tool_input("bash", {"command": cmd})
        assert result.endswith("\u2026)")
        inner = result[3:-1]  # strip '($ ' and ')'
        assert len(inner) <= 80

    def test_bash_command_truncated_at_77_plus_ellipsis(self) -> None:
        cmd = "a" * 100
        result = _summarise_tool_input("bash", {"command": cmd})
        # First 77 chars + ellipsis
        expected = f"($ {'a' * 77}\u2026)"
        assert result == expected


# ---------------------------------------------------------------------------
# Search/pattern tools
# ---------------------------------------------------------------------------

class TestSearchTools:
    """Tools that have a 'pattern' key."""

    def test_grep_search_shows_pattern(self) -> None:
        result = _summarise_tool_input(
            "grep_search", {"pattern": "def foo", "limit": 10, "context_lines": 2}
        )
        assert result == "(pattern='def foo')"

    def test_find_replace_shows_pattern(self) -> None:
        result = _summarise_tool_input("find_replace", {"pattern": "old_func", "replacement": "new_func"})
        assert result == "(pattern='old_func')"

    def test_glob_search_no_pattern_key_uses_fallback(self) -> None:
        # glob_search uses 'pattern' key too
        result = _summarise_tool_input("glob_search", {"pattern": "*.py", "limit": 20})
        assert result == "(pattern='*.py')"


# ---------------------------------------------------------------------------
# Fallback: first key=value pair
# ---------------------------------------------------------------------------

class TestFallbackHandler:
    """Fallback behavior when no specific handler matches."""

    def test_single_key_shown_as_kv_pair(self) -> None:
        result = _summarise_tool_input("unknown_tool", {"foo": "bar"})
        assert result == "(foo='bar')"

    def test_first_key_used_when_multiple_keys(self) -> None:
        # Only the first key is shown
        result = _summarise_tool_input("unknown_tool", {"alpha": "1", "beta": "2"})
        assert result == "(alpha='1')"

    def test_empty_params_returns_empty_string(self) -> None:
        result = _summarise_tool_input("any_tool", {})
        assert result == ""

    def test_fallback_value_truncated_at_60_chars(self) -> None:
        long_val = "x" * 100
        result = _summarise_tool_input("tool", {"key": long_val})
        assert "\u2026" in result
        # Inner portion: key='<57 chars>...'
        inner = result[1:-1]  # strip outer parens
        assert inner.startswith("key=")

    def test_fallback_value_exactly_60_chars_not_truncated(self) -> None:
        val = "x" * 60
        result = _summarise_tool_input("tool", {"key": val})
        assert result == f"(key={val!r})"
        assert "\u2026" not in result

    def test_fallback_value_at_61_chars_truncated(self) -> None:
        val = "x" * 61
        result = _summarise_tool_input("tool", {"key": val})
        assert "\u2026" in result

    def test_batch_read_paths_shown_via_fallback(self) -> None:
        # batch_read has 'paths' key (not 'path') so goes through fallback
        result = _summarise_tool_input("batch_read", {"paths": ["a.py", "b.py"], "limit": 100})
        assert "paths" in result
        assert "a.py" in result

    def test_python_eval_snippet_shown_via_fallback(self) -> None:
        result = _summarise_tool_input("python_eval", {"snippet": "x = 1"})
        assert "snippet" in result
        assert "x = 1" in result

    def test_batch_write_files_shown_via_fallback(self) -> None:
        result = _summarise_tool_input("batch_write", {"files": [{"path": "f.py", "content": "x"}]})
        assert "files" in result


# ---------------------------------------------------------------------------
# Specific compound tools
# ---------------------------------------------------------------------------

class TestCompoundTools:
    """Tools with complex parameter structures."""

    def test_batch_edit_edits_key_fallback(self) -> None:
        # batch_edit has 'edits' key -> fallback
        result = _summarise_tool_input(
            "batch_edit",
            {"edits": [{"path": "a.py", "old_str": "x", "new_str": "y"}]}
        )
        assert "edits" in result

    def test_file_info_paths_key_fallback(self) -> None:
        result = _summarise_tool_input("file_info", {"paths": ["a.py"]})
        assert "paths" in result

    def test_tool_with_only_boolean_param(self) -> None:
        result = _summarise_tool_input("git_status", {})
        assert result == ""

    def test_non_string_value_converted_to_str(self) -> None:
        result = _summarise_tool_input("tool", {"count": 42})
        # Should stringify the integer
        assert "count" in result
        assert "42" in result


# ---------------------------------------------------------------------------
# Return-type guarantees
# ---------------------------------------------------------------------------

class TestReturnType:
    """_summarise_tool_input always returns a str."""

    def test_always_returns_str(self) -> None:
        cases = [
            ("bash", {"command": "ls"}),
            ("edit_file", {"path": "x.py"}),
            ("grep_search", {"pattern": "foo", "limit": 5, "context_lines": 0}),
            ("unknown", {}),
            ("unknown", {"key": "val"}),
        ]
        for tool_name, params in cases:
            result = _summarise_tool_input(tool_name, params)
            assert isinstance(result, str), f"Expected str for {tool_name!r}, got {type(result)}"
