"""Backward-compatibility shim — code moved to harness.evaluation.dual_evaluator.

All imports from ``harness.dual_evaluator`` continue to work via this re-export.
New code should import from ``harness.evaluation.dual_evaluator`` directly.
"""
# ruff: noqa: F401, F403
from harness.evaluation.dual_evaluator import (
    DualEvaluator,
    parse_score,
)

__all__ = [
    "DualEvaluator",
    "parse_score",
]
