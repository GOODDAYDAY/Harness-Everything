"""Backward-compatibility shim — code moved to harness.evaluation.evaluator.

All imports from ``harness.evaluator`` continue to work via this re-export.
New code should import from ``harness.evaluation.evaluator`` directly.
"""
# ruff: noqa: F401, F403
from harness.evaluation.evaluator import (
    Evaluator,
    Verdict,
    _build_log_summary,
    _extract_before_snapshots,
    _extract_executor_status,
    _is_tool_error,
)

__all__ = [
    "Evaluator",
    "Verdict",
    "_build_log_summary",
    "_extract_before_snapshots",
    "_extract_executor_status",
    "_is_tool_error",
]
