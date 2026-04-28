"""json_transform — JSON parse, query, validate, merge, and diff tool.

Provides structured operations on JSON data without any external
dependencies (no ``jsonpath-ng``, ``jsonschema``, etc.).

Operations (controlled by the ``op`` parameter)
------------------------------------------------
* **parse**    — Parse a JSON string and pretty-print it. Validates syntax
                 and reports the error position on failure.
* **query**    — Extract a nested value using a simple dot/bracket path
                 notation (e.g. ``"foo.bar[2].name"``).  No external
                 JSONPath library required.
* **validate** — Check that a JSON value conforms to a basic JSON Schema
                 (supports ``type``, ``required``, ``properties``,
                 ``items``, ``minLength``, ``maxLength``, ``minimum``,
                 ``maximum``, ``enum``).
* **merge**    — Deep-merge two JSON objects.  Right-hand values win on
                 key conflicts; nested dicts are merged recursively.
* **diff**     — Produce a structural diff between two JSON values,
                 showing added/removed/changed leaf paths.

Design goals
------------
* Zero external dependencies — pure stdlib ``json``, ``re``, ``copy``.
* LLM-friendly output — results are plain text or compact JSON.
* Fail-safe — all parse errors and invalid paths return ``ToolResult``
  with ``is_error=True`` and a descriptive message.
"""

from __future__ import annotations

import copy
import json as _json
import re
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

_MAX_OUTPUT_CHARS = 24_000


# ---------------------------------------------------------------------------
# Path query helper
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> list[str | int]:
    """Parse a dot/bracket path string into a list of keys/indices.

    Examples::

        "foo"            → ["foo"]
        "foo.bar"        → ["foo", "bar"]
        "foo[0]"         → ["foo", 0]
        "foo.bar[2].baz" → ["foo", "bar", 2, "baz"]
        "a.b[0][1]"      → ["a", "b", 0, 1]
    """
    if not path:
        return []
    # Normalize: convert bracket notation to .N so we can split on "."
    # "foo[0]" → "foo.0", "a[1][2]" → "a.1.2"
    normalized = re.sub(r"\[(\d+)\]", r".\1", path)
    parts: list[str | int] = []
    for segment in normalized.split("."):
        segment = segment.strip()
        if not segment:
            continue
        if segment.isdigit():
            parts.append(int(segment))
        else:
            parts.append(segment)
    return parts


def _query_path(obj: Any, parts: list[str | int]) -> tuple[bool, Any, str]:
    """Walk *obj* following *parts* and return ``(found, value, error)``.

    Returns ``(True, value, "")`` on success or ``(False, None, msg)`` on
    failure (key missing, index out of range, wrong type).
    """
    current = obj
    for i, part in enumerate(parts):
        if isinstance(part, int):
            if not isinstance(current, list):
                return (
                    False,
                    None,
                    f"Expected list at path step {i} ({part!r}) "
                    f"but got {type(current).__name__}",
                )
            if part < 0 or part >= len(current):
                return (
                    False,
                    None,
                    f"Index {part} out of range (list length {len(current)}) "
                    f"at path step {i}",
                )
            current = current[part]
        else:
            if not isinstance(current, dict):
                return (
                    False,
                    None,
                    f"Expected object at path step {i} ({part!r}) "
                    f"but got {type(current).__name__}",
                )
            if part not in current:
                available = sorted(current.keys())[:10]
                return (
                    False,
                    None,
                    f"Key {part!r} not found at path step {i}. "
                    f"Available keys: {available}",
                )
            current = current[part]
    return True, current, ""


# ---------------------------------------------------------------------------
# Basic JSON Schema validator
# ---------------------------------------------------------------------------


def _validate_schema(
    value: Any,
    schema: dict,
    path: str = "$",
) -> list[str]:
    """Validate *value* against a simple JSON Schema dict.

    Returns a list of error message strings (empty list = valid).

    Supported keywords: ``type``, ``required``, ``properties``, ``items``,
    ``minLength``, ``maxLength``, ``minimum``, ``maximum``, ``enum``,
    ``minItems``, ``maxItems``.
    """
    errors: list[str] = []

    # type check
    type_map: dict[str, type | tuple] = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    expected_type = schema.get("type")
    if expected_type:
        py_type = type_map.get(expected_type)
        if py_type is not None:
            # booleans are int subclasses in Python — disambiguate
            if expected_type == "integer" and isinstance(value, bool):
                errors.append(f"{path}: expected integer but got boolean")
            elif expected_type == "number" and isinstance(value, bool):
                errors.append(f"{path}: expected number but got boolean")
            elif not isinstance(value, py_type):  # type: ignore[arg-type]
                errors.append(
                    f"{path}: expected {expected_type} but got {type(value).__name__}"
                )

    # enum
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: {value!r} not in enum {schema['enum']}")

    # string constraints
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(
                f"{path}: string length {len(value)} < minLength {schema['minLength']}"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(
                f"{path}: string length {len(value)} > maxLength {schema['maxLength']}"
            )

    # numeric constraints
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")

    # array constraints
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(
                f"{path}: array length {len(value)} < minItems {schema['minItems']}"
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(
                f"{path}: array length {len(value)} > maxItems {schema['maxItems']}"
            )
        if "items" in schema and isinstance(schema["items"], dict):
            for idx, item in enumerate(value):
                errors.extend(
                    _validate_schema(item, schema["items"], f"{path}[{idx}]")
                )

    # object constraints
    if isinstance(value, dict):
        required = schema.get("required", [])
        for req_key in required:
            if req_key not in value:
                errors.append(f"{path}: missing required property {req_key!r}")
        props = schema.get("properties", {})
        for prop_key, prop_schema in props.items():
            if prop_key in value:
                errors.extend(
                    _validate_schema(value[prop_key], prop_schema, f"{path}.{prop_key}")
                )

    return errors


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


def _deep_merge(base: Any, override: Any) -> Any:
    """Recursively merge *override* into *base*.

    * Both dicts: merge recursively; override wins on scalar conflicts.
    * Otherwise: override wins unconditionally.
    Returns a new object (does not mutate either input).
    """
    if isinstance(base, dict) and isinstance(override, dict):
        result = copy.deepcopy(base)
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = _deep_merge(result[key], val)
            else:
                result[key] = copy.deepcopy(val)
        return result
    return copy.deepcopy(override)


# ---------------------------------------------------------------------------
# Structural diff
# ---------------------------------------------------------------------------


def _diff(
    left: Any,
    right: Any,
    path: str = "$",
    changes: list[dict] | None = None,
) -> list[dict]:
    """Produce a flat list of ``{path, op, left, right}`` diff entries.

    Operations: ``added`` (key in right only), ``removed`` (key in left only),
    ``changed`` (both have key but different scalar values), ``type_changed``.
    Recursively descends into dicts; lists are compared element-by-element.
    """
    if changes is None:
        changes = []

    if type(left) != type(right):  # noqa: E721
        changes.append({"path": path, "op": "type_changed",
                         "left": type(left).__name__,
                         "right": type(right).__name__})
        return changes

    if isinstance(left, dict):
        all_keys = set(left) | set(right)
        for key in sorted(all_keys):
            child_path = f"{path}.{key}"
            if key not in right:
                changes.append({"path": child_path, "op": "removed",
                                 "left": left[key], "right": None})
            elif key not in left:
                changes.append({"path": child_path, "op": "added",
                                 "left": None, "right": right[key]})
            else:
                _diff(left[key], right[key], child_path, changes)

    elif isinstance(left, list):
        max_len = max(len(left), len(right))
        for i in range(max_len):
            child_path = f"{path}[{i}]"
            if i >= len(left):
                changes.append({"path": child_path, "op": "added",
                                 "left": None, "right": right[i]})
            elif i >= len(right):
                changes.append({"path": child_path, "op": "removed",
                                 "left": left[i], "right": None})
            else:
                _diff(left[i], right[i], child_path, changes)

    else:
        # Scalar: compare directly
        if left != right:
            changes.append({"path": path, "op": "changed",
                             "left": left, "right": right})

    return changes


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class JsonTransformTool(Tool):
    """Parse, query, validate, merge, and diff JSON data.

    Five operations (``op`` parameter):

    **parse**
        Parse a JSON string and return pretty-printed output with type
        and size information. Reports syntax errors with position.

    **query**
        Extract a nested value from a parsed JSON object using a simple
        dot/bracket path (e.g. ``"results[0].name"``).

    **validate**
        Check a JSON value against a basic JSON Schema (supports
        ``type``, ``required``, ``properties``, ``items``, ``enum``,
        ``minimum``, ``maximum``, ``minLength``, ``maxLength``).

    **merge**
        Deep-merge two JSON objects. The right-hand object's values win
        on conflicts; nested objects are merged recursively.

    **diff**
        Compute a structural diff between two JSON values and return a
        flat list of added/removed/changed paths.
    """

    name = "json_transform"
    description = (
        "Parse, query, validate, merge, and diff JSON data. "
        "op='parse': parse and pretty-print a JSON string; "
        "op='query': extract a value by dot/bracket path (e.g. 'foo.bar[0].id'); "
        "op='validate': check JSON against a basic schema; "
        "op='merge': deep-merge two JSON objects (right wins on conflicts); "
        "op='diff': show added/removed/changed paths between two JSON values. "
        "No external dependencies — pure stdlib."
    )
    requires_path_check = False  # no filesystem access
    tags = frozenset({"file_read"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["parse", "query", "validate", "merge", "diff"],
                    "description": (
                        "Operation to perform: "
                        "'parse' | 'query' | 'validate' | 'merge' | 'diff'. "
                        "Default: 'parse'."
                    ),
                    "default": "parse",
                },
                "data": {
                    "type": ["string", "object", "array", "number", "boolean", "null"],
                    "description": (
                        "Primary input. For 'parse': a JSON string. "
                        "For 'query'/'validate': a JSON string or already-parsed value. "
                        "For 'merge'/'diff': the LEFT / base value (JSON string or object)."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Dot/bracket path for 'query' op "
                        "(e.g. 'results[0].name', 'meta.count'). "
                        "Use '' or omit to return the root value."
                    ),
                    "default": "",
                },
                "schema": {
                    "type": ["object", "string"],
                    "description": (
                        "JSON Schema dict (or JSON string) for 'validate' op. "
                        "Supports: type, required, properties, items, enum, "
                        "minimum, maximum, minLength, maxLength, minItems, maxItems."
                    ),
                },
                "other": {
                    "type": ["string", "object", "array", "number", "boolean", "null"],
                    "description": (
                        "Secondary input for 'merge' and 'diff' ops: "
                        "the RIGHT / override value (JSON string or object). "
                        "For 'merge', this overrides 'data' on conflicts."
                    ),
                },
                "indent": {
                    "type": "integer",
                    "description": "Indentation spaces for pretty-print output (default: 2).",
                    "default": 2,
                    "minimum": 0,
                    "maximum": 8,
                },
            },
            "required": ["data"],
        }

    async def execute(
        self,
        config: HarnessConfig,
        *,
        op: str = "parse",
        data: Any = None,
        path: str = "",
        schema: Any = None,
        other: Any = None,
        indent: int = 2,
    ) -> ToolResult:
        op = (op or "parse").strip().lower()
        if op not in ("parse", "query", "validate", "merge", "diff"):
            return ToolResult(
                error=(
                    f"Unknown op {op!r}. "
                    "Use: parse / query / validate / merge / diff"
                ),
                is_error=True,
            )

        if data is None:
            return ToolResult(error="'data' is required", is_error=True)

        indent = max(0, min(8, indent))

        # ------------------------------------------------------------------
        # Shared helper: coerce a value to a parsed Python object.
        # If it's already a dict/list/etc., pass through.
        # If it's a string, parse as JSON.
        # ------------------------------------------------------------------
        def _coerce(val: Any, label: str) -> tuple[bool, Any, str]:
            if isinstance(val, str):
                try:
                    return True, _json.loads(val), ""
                except _json.JSONDecodeError as exc:
                    return False, None, f"JSON parse error in {label}: {exc}"
            return True, val, ""

        # ---- parse -------------------------------------------------------
        if op == "parse":
            if not isinstance(data, str):
                # Already a Python object — just re-serialize
                parsed = data
            else:
                try:
                    parsed = _json.loads(data)
                except _json.JSONDecodeError as exc:
                    return ToolResult(
                        error=f"JSON syntax error: {exc}",
                        is_error=True,
                    )

            pretty = _json.dumps(parsed, indent=indent, ensure_ascii=False)
            # Determine type and size info
            if isinstance(parsed, dict):
                type_desc = f"object with {len(parsed)} key(s)"
            elif isinstance(parsed, list):
                type_desc = f"array with {len(parsed)} element(s)"
            else:
                type_desc = type(parsed).__name__

            header = f"[JSON {type_desc} — {len(pretty)} chars]\n"
            output = header + pretty
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
            return ToolResult(output=output)

        # ---- query -------------------------------------------------------
        if op == "query":
            ok, parsed, err = _coerce(data, "data")
            if not ok:
                return ToolResult(error=err, is_error=True)

            parts = _parse_path(path)
            found, value, err_msg = _query_path(parsed, parts)
            if not found:
                return ToolResult(error=f"Query failed: {err_msg}", is_error=True)

            if isinstance(value, (dict, list)):
                result_text = _json.dumps(value, indent=indent, ensure_ascii=False)
            else:
                result_text = _json.dumps(value, ensure_ascii=False)

            if len(result_text) > _MAX_OUTPUT_CHARS:
                result_text = result_text[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
            return ToolResult(output=result_text)

        # ---- validate ----------------------------------------------------
        if op == "validate":
            if schema is None:
                return ToolResult(
                    error="'schema' is required for op='validate'",
                    is_error=True,
                )
            ok, parsed, err = _coerce(data, "data")
            if not ok:
                return ToolResult(error=err, is_error=True)

            ok_s, parsed_schema, err_s = _coerce(schema, "schema")
            if not ok_s:
                return ToolResult(error=err_s, is_error=True)
            if not isinstance(parsed_schema, dict):
                return ToolResult(
                    error="'schema' must be a JSON object (dict)",
                    is_error=True,
                )

            errors = _validate_schema(parsed, parsed_schema)
            result = {
                "valid": len(errors) == 0,
                "error_count": len(errors),
                "errors": errors,
            }
            return ToolResult(output=_json.dumps(result, indent=indent))

        # ---- merge -------------------------------------------------------
        if op == "merge":
            if other is None:
                return ToolResult(
                    error="'other' is required for op='merge'",
                    is_error=True,
                )
            ok1, left, err1 = _coerce(data, "data")
            if not ok1:
                return ToolResult(error=err1, is_error=True)
            ok2, right, err2 = _coerce(other, "other")
            if not ok2:
                return ToolResult(error=err2, is_error=True)

            merged = _deep_merge(left, right)
            output = _json.dumps(merged, indent=indent, ensure_ascii=False)
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
            return ToolResult(output=output)

        # ---- diff --------------------------------------------------------
        if op == "diff":
            if other is None:
                return ToolResult(
                    error="'other' is required for op='diff'",
                    is_error=True,
                )
            ok1, left, err1 = _coerce(data, "data")
            if not ok1:
                return ToolResult(error=err1, is_error=True)
            ok2, right, err2 = _coerce(other, "other")
            if not ok2:
                return ToolResult(error=err2, is_error=True)

            changes = _diff(left, right)
            result = {
                "changes_total": len(changes),
                "changes": changes,
            }
            output = _json.dumps(result, indent=indent, ensure_ascii=False)
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... [truncated]"
            return ToolResult(output=output)

        # Should never reach here
        return ToolResult(error=f"Unhandled op: {op}", is_error=True)
