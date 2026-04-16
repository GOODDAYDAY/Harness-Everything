"""harness.pipeline — pipeline orchestration components.

Subpackage containing:
- ``pipeline_loop``  — PipelineLoop outer-rounds orchestrator
- ``phase_runner``   — PhaseRunner single-phase executor
- ``phase``          — PhaseConfig, PhaseResult, InnerResult data classes

Re-exports the public API so that both old-style imports
(``from harness.pipeline import PipelineLoop``) and new imports
(``from harness.pipeline.pipeline_loop import PipelineLoop``) work.
"""

from harness.pipeline.phase import (
    DualScore,
    InnerResult,
    PhaseConfig,
    PhaseResult,
    ScoreItem,
)
from harness.pipeline.phase_runner import PhaseRunner
from harness.pipeline.pipeline_loop import PipelineLoop, PipelineResult

__all__ = [
    "DualScore",
    "InnerResult",
    "PhaseConfig",
    "PhaseResult",
    "PhaseRunner",
    "PipelineLoop",
    "PipelineResult",
    "ScoreItem",
]
