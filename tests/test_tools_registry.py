"""Tests for the harness tools registry structural integrity.

These checks were previously hardcoded import-time assertions in
harness/tools/__init__.py (as _EXPECTED_DEFAULT_COUNT / _EXPECTED_OPTIONAL_COUNT).
Moving them here keeps the safety net without causing an AssertionError cascade
that breaks all modes on every import when a tool is added or removed.
"""

# Import core.config first to resolve the shim circular-import ordering issue.
import harness.core.config  # noqa: F401 — side-effect import breaks the circular chain
from harness.tools import DEFAULT_TOOLS, OPTIONAL_TOOLS


def test_no_duplicate_tool_names():
    names = [t.name for t in DEFAULT_TOOLS + OPTIONAL_TOOLS]
    assert len(names) == len(set(names)), f"Duplicate tool names: {sorted(names)}"


def test_all_tools_implement_abc():
    from harness.tools.base import Tool

    for tool in DEFAULT_TOOLS + OPTIONAL_TOOLS:
        assert isinstance(tool, Tool), f"{tool!r} is not a Tool instance"
        assert callable(tool.input_schema), f"{tool.name}.input_schema is not callable"
        assert callable(tool.execute), f"{tool.name}.execute is not callable"
