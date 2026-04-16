"""Backward-compatibility shim — code moved to harness.pipeline.phase_runner.

All imports from ``harness.phase_runner`` continue to work via this re-export.
New code should import from ``harness.pipeline.phase_runner`` directly.
"""
# ruff: noqa: F401, F403
from harness.pipeline.phase_runner import (
    MIN_SYNTHESIS_CHARS,
    PhaseRunner,
    _read_source_files,
    _tokenise_path,
    _tokenise_phrase,
    _truncate_file_content,
    score_file_relevance,
)

__all__ = [
    "MIN_SYNTHESIS_CHARS",
    "PhaseRunner",
    "_read_source_files",
    "_tokenise_path",
    "_tokenise_phrase",
    "_truncate_file_content",
    "score_file_relevance",
]
