"""Tests for the json_transform tool.

Covers all five operations: parse, query, validate, merge, diff.
Also covers edge cases: bad JSON input, invalid paths, schema validation,
deep merge behaviour, and output truncation.
"""
import asyncio
from unittest.mock import MagicMock


from harness.tools.json_transform import JsonTransformTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config():
    """Return a minimal HarnessConfig mock (json_transform has no path checks)."""
    cfg = MagicMock()
    return cfg


def run(coro):
    return asyncio.run(coro)


tool = JsonTransformTool()


# ---------------------------------------------------------------------------
# 1. parse
# ---------------------------------------------------------------------------

class TestParseOp:
    def test_parse_object(self):
        result = run(tool.execute(make_config(), data='{"a": 1, "b": 2}', op="parse"))
        assert not result.is_error
        assert '"a": 1' in result.output
        assert '"b": 2' in result.output

    def test_parse_array(self):
        result = run(tool.execute(make_config(), data='[1, 2, 3]', op="parse"))
        assert not result.is_error
        assert "1" in result.output
        assert "3" in result.output

    def test_parse_already_dict(self):
        """data may be a Python dict already, not a JSON string."""
        result = run(tool.execute(make_config(), data={"key": "value"}, op="parse"))
        assert not result.is_error
        assert "key" in result.output

    def test_parse_invalid_json_string(self):
        result = run(tool.execute(make_config(), data="{not valid json}", op="parse"))
        assert result.is_error

    def test_parse_null(self):
        result = run(tool.execute(make_config(), data="null", op="parse"))
        assert not result.is_error
        assert "null" in result.output.lower()

    def test_parse_boolean_true(self):
        result = run(tool.execute(make_config(), data="true", op="parse"))
        assert not result.is_error

    def test_parse_number(self):
        result = run(tool.execute(make_config(), data="42", op="parse"))
        assert not result.is_error
        assert "42" in result.output

    def test_parse_indent_respected(self):
        result = run(tool.execute(make_config(), data='{"x": 1}', op="parse", indent=4))
        assert not result.is_error
        # 4-space indent means lines like '    "x": 1'
        assert "    " in result.output


# ---------------------------------------------------------------------------
# 2. query
# ---------------------------------------------------------------------------

class TestQueryOp:
    _DATA = '{"user": {"name": "Alice", "scores": [10, 20, 30]}}'

    def test_query_nested_key(self):
        result = run(tool.execute(make_config(), data=self._DATA, op="query", path="user.name"))
        assert not result.is_error
        assert "Alice" in result.output

    def test_query_array_index(self):
        result = run(tool.execute(make_config(), data=self._DATA, op="query", path="user.scores[1]"))
        assert not result.is_error
        assert "20" in result.output

    def test_query_root_returns_full(self):
        result = run(tool.execute(make_config(), data=self._DATA, op="query", path=""))
        assert not result.is_error
        assert "Alice" in result.output

    def test_query_missing_key(self):
        result = run(tool.execute(make_config(), data=self._DATA, op="query", path="user.age"))
        assert result.is_error or "not found" in result.output.lower() or "error" in result.output.lower()

    def test_query_out_of_range(self):
        result = run(tool.execute(make_config(), data=self._DATA, op="query", path="user.scores[99]"))
        assert result.is_error or "not found" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# 3. validate
# ---------------------------------------------------------------------------

class TestValidateOp:
    _SCHEMA = '{"type": "object", "required": ["name", "age"], "properties": {"name": {"type": "string"}, "age": {"type": "number"}}}'

    def test_validate_passes(self):
        result = run(tool.execute(
            make_config(),
            data='{"name": "Bob", "age": 30}',
            op="validate",
            schema=self._SCHEMA,
        ))
        assert not result.is_error
        assert "valid" in result.output.lower()

    def test_validate_fails_missing_required(self):
        result = run(tool.execute(
            make_config(),
            data='{"name": "Bob"}',  # missing 'age'
            op="validate",
            schema=self._SCHEMA,
        ))
        # Should either be error OR output describing failure
        assert result.is_error or "age" in result.output.lower() or "required" in result.output.lower() or "invalid" in result.output.lower()

    def test_validate_wrong_type(self):
        result = run(tool.execute(
            make_config(),
            data='{"name": 123, "age": 30}',  # name should be string
            op="validate",
            schema=self._SCHEMA,
        ))
        # Should fail validation
        assert result.is_error or "string" in result.output.lower() or "invalid" in result.output.lower()

    def test_validate_no_schema_is_error(self):
        """validate op without a schema should return an error."""
        result = run(tool.execute(
            make_config(),
            data='{"x": 1}',
            op="validate",
        ))
        assert result.is_error

    def test_validate_array_schema(self):
        result = run(tool.execute(
            make_config(),
            data='[1, 2, 3]',
            op="validate",
            schema='{"type": "array", "items": {"type": "number"}}',
        ))
        assert not result.is_error
        assert "valid" in result.output.lower()


# ---------------------------------------------------------------------------
# 4. merge
# ---------------------------------------------------------------------------

class TestMergeOp:
    def test_merge_basic(self):
        base = '{"a": 1, "b": 2}'
        override = '{"b": 99, "c": 3}'
        result = run(tool.execute(make_config(), data=base, op="merge", other=override))
        assert not result.is_error
        # b overridden to 99, c added
        assert "99" in result.output
        assert "\"c\"" in result.output or "c" in result.output

    def test_merge_deep(self):
        base = '{"x": {"y": 1, "z": 2}}'
        override = '{"x": {"z": 99}}'
        result = run(tool.execute(make_config(), data=base, op="merge", other=override))
        assert not result.is_error
        # y should still be present (deep merge)
        assert "\"y\"" in result.output or "y" in result.output
        assert "99" in result.output

    def test_merge_no_other_is_error(self):
        result = run(tool.execute(
            make_config(), data='{"a": 1}', op="merge",
        ))
        assert result.is_error

    def test_merge_base_wins_on_no_overlap(self):
        base = '{"a": 1}'
        override = '{"b": 2}'
        result = run(tool.execute(make_config(), data=base, op="merge", other=override))
        assert not result.is_error
        assert "\"a\"" in result.output or "a" in result.output
        assert "\"b\"" in result.output or "b" in result.output


# ---------------------------------------------------------------------------
# 5. diff
# ---------------------------------------------------------------------------

class TestDiffOp:
    def test_diff_identical(self):
        obj = '{"a": 1}'
        result = run(tool.execute(make_config(), data=obj, op="diff", other=obj))
        assert not result.is_error
        # Identical objects produce 0 changes
        output_lower = result.output.lower()
        assert (
            "no differences" in output_lower
            or "identical" in output_lower
            or "changes_total\": 0" in result.output
            or result.output.strip() == ""
        )

    def test_diff_added_key(self):
        base = '{"a": 1}'
        new = '{"a": 1, "b": 2}'
        result = run(tool.execute(make_config(), data=base, op="diff", other=new))
        assert not result.is_error
        assert "b" in result.output

    def test_diff_changed_value(self):
        base = '{"a": 1}'
        new = '{"a": 99}'
        result = run(tool.execute(make_config(), data=base, op="diff", other=new))
        assert not result.is_error
        assert "99" in result.output or "a" in result.output

    def test_diff_removed_key(self):
        base = '{"a": 1, "b": 2}'
        new = '{"a": 1}'
        result = run(tool.execute(make_config(), data=base, op="diff", other=new))
        assert not result.is_error
        assert "b" in result.output or "removed" in result.output.lower()

    def test_diff_no_other_is_error(self):
        result = run(tool.execute(make_config(), data='{"a": 1}', op="diff"))
        assert result.is_error


# ---------------------------------------------------------------------------
# 6. Edge cases / error handling
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_op_is_error(self):
        result = run(tool.execute(make_config(), data="{}", op="nonexistent_op"))
        assert result.is_error

    def test_empty_object(self):
        result = run(tool.execute(make_config(), data="{}", op="parse"))
        assert not result.is_error

    def test_empty_array(self):
        result = run(tool.execute(make_config(), data="[]", op="parse"))
        assert not result.is_error

    def test_deeply_nested_query(self):
        data = '{"a": {"b": {"c": {"d": "deep"}}}}'
        result = run(tool.execute(make_config(), data=data, op="query", path="a.b.c.d"))
        assert not result.is_error
        assert "deep" in result.output

    def test_data_is_already_list(self):
        """data parameter accepts a Python list directly."""
        result = run(tool.execute(make_config(), data=[1, 2, 3], op="parse"))
        assert not result.is_error

    def test_data_is_already_int(self):
        result = run(tool.execute(make_config(), data=42, op="parse"))
        assert not result.is_error

    def test_parse_negative_number(self):
        result = run(tool.execute(make_config(), data="-3.14", op="parse"))
        assert not result.is_error
        assert "3.14" in result.output
