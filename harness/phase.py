"""Backward-compatibility shim — code moved to harness.pipeline.phase.

All imports from ``harness.phase`` continue to work via this re-export.
New code should import from ``harness.pipeline.phase`` directly.
"""
# ruff: noqa: F401, F403
from harness.pipeline.phase import (
    DualScore,
    InnerResult,
    PhaseConfig,
    PhaseResult,
    ScoreItem,
)

__all__ = [
    "DualScore",
    "InnerResult",
    "PhaseConfig",
    "PhaseResult",
    "ScoreItem",
]
