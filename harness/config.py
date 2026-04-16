"""Backward-compatibility shim — code moved to harness.core.config.

All imports from ``harness.config`` continue to work via this re-export.
New code should import from ``harness.core.config`` directly.
"""
# ruff: noqa: F401, F403
from harness.core.config import (
    DualEvaluatorConfig,
    EvaluatorConfig,
    HarnessConfig,
    PipelineConfig,
    PlannerConfig,
)

__all__ = [
    "DualEvaluatorConfig",
    "EvaluatorConfig",
    "HarnessConfig",
    "PipelineConfig",
    "PlannerConfig",
]
