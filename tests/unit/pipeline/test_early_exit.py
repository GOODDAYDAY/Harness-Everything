"""Tests for inner-round early-exit feature in implement mode.

Covers:
  - PipelineConfig.inner_early_exit_threshold: default, valid values, validation
  - PhaseConfig.inner_early_exit_threshold: default None (inherit), override
  - Early-exit loop logic: triggered, not triggered, per-phase override, edge cases
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.core.config import PipelineConfig
from harness.pipeline.phase import PhaseConfig


# ---------------------------------------------------------------------------
# PipelineConfig validation
# ---------------------------------------------------------------------------


def test_pipeline_config_default_threshold_is_disabled():
    """Default inner_early_exit_threshold must be 0.0 (disabled)."""
    cfg = PipelineConfig()
    assert cfg.inner_early_exit_threshold == 0.0


def test_pipeline_config_valid_threshold_accepted():
    """Valid thresholds in [0.0, 10.0] should be accepted without error."""
    for v in (0.0, 7.0, 8.5, 9.0, 10.0):
        cfg = PipelineConfig(inner_early_exit_threshold=v)
        assert cfg.inner_early_exit_threshold == v


def test_pipeline_config_rejects_negative_threshold():
    """Negative threshold must raise ValueError."""
    with pytest.raises(ValueError, match="inner_early_exit_threshold"):
        PipelineConfig(inner_early_exit_threshold=-0.1)


def test_pipeline_config_rejects_threshold_above_10():
    """Threshold > 10.0 must raise ValueError."""
    with pytest.raises(ValueError, match="inner_early_exit_threshold"):
        PipelineConfig(inner_early_exit_threshold=10.1)


# ---------------------------------------------------------------------------
# PhaseConfig early-exit threshold override field
# ---------------------------------------------------------------------------


def _make_phase(**kw: Any) -> PhaseConfig:
    return PhaseConfig(name="test", index=0, system_prompt="do stuff", **kw)


def test_phase_config_default_threshold_is_none():
    """PhaseConfig.inner_early_exit_threshold defaults to None (inherit pipeline default)."""
    assert _make_phase().inner_early_exit_threshold is None


def test_phase_config_threshold_explicit_override():
    """PhaseConfig.inner_early_exit_threshold accepts an explicit float override."""
    phase = _make_phase(inner_early_exit_threshold=9.0)
    assert phase.inner_early_exit_threshold == 9.0


def test_phase_config_threshold_explicit_disable():
    """Phase can explicitly disable early-exit by setting 0.0."""
    phase = _make_phase(inner_early_exit_threshold=0.0)
    assert phase.inner_early_exit_threshold == 0.0


# ---------------------------------------------------------------------------
# Helper: simulate the implement-mode early-exit loop logic
# ---------------------------------------------------------------------------
# We extract the core loop logic directly (mirroring phase_runner.py) so we
# can unit-test it without instantiating PhaseRunner (which requires LLM/DB).


def _run_loop(
    scores: list[float],
    pipeline_threshold: float = 0.0,
    phase_threshold: float | None = None,
) -> list[float]:
    """
    Simulate the implement-mode inner loop from phase_runner.py.

    Returns the list of combined_scores for rounds that actually executed.
    """
    n = len(scores)

    # Build minimal config objects
    pipeline_cfg = PipelineConfig(inner_early_exit_threshold=pipeline_threshold)
    phase = _make_phase(
        mode="implement",
        inner_early_exit_threshold=phase_threshold,
    )

    # Determine effective threshold (mirrors phase_runner.py logic)
    _phase_threshold = getattr(phase, "inner_early_exit_threshold", None)
    _eet = (
        _phase_threshold
        if _phase_threshold is not None
        else pipeline_cfg.inner_early_exit_threshold
    )

    executed: list[float] = []
    best_score: float | None = None

    for inner, score in enumerate(scores):
        executed.append(score)
        if best_score is None or score > best_score:
            best_score = score

        # Early-exit logic (exact copy of phase_runner.py)
        if (
            _eet > 0.0
            and best_score is not None
            and best_score >= _eet
            and inner < n - 1
        ):
            break

    return executed


# ---------------------------------------------------------------------------
# Early-exit loop behaviour tests
# ---------------------------------------------------------------------------


def test_early_exit_disabled_runs_all_rounds():
    """When threshold is 0.0, all inner rounds run even with perfect scores."""
    ran = _run_loop(scores=[9.5, 9.8, 10.0], pipeline_threshold=0.0)
    assert len(ran) == 3


def test_early_exit_triggers_after_first_round():
    """Round 1 score >= threshold → only 1 round executes."""
    ran = _run_loop(scores=[9.0, 5.0, 5.0], pipeline_threshold=8.5)
    assert ran == [9.0]


def test_early_exit_triggers_after_second_round():
    """Round 1 below threshold, round 2 meets it → 2 rounds execute."""
    ran = _run_loop(scores=[6.0, 9.0, 5.0], pipeline_threshold=8.5)
    assert ran == [6.0, 9.0]


def test_no_early_exit_when_all_below_threshold():
    """When no round meets the threshold, all rounds run."""
    ran = _run_loop(scores=[7.0, 7.5, 8.0], pipeline_threshold=8.5)
    assert len(ran) == 3


def test_early_exit_exactly_at_threshold():
    """A score exactly equal to the threshold must trigger early exit (>=)."""
    ran = _run_loop(scores=[8.5, 5.0, 5.0], pipeline_threshold=8.5)
    assert ran == [8.5]


def test_no_early_exit_on_last_round():
    """Meeting the threshold on the last round is a no-op — nothing to skip."""
    ran = _run_loop(scores=[5.0, 5.0, 9.0], pipeline_threshold=8.5)
    assert ran == [5.0, 5.0, 9.0]


def test_phase_threshold_zero_disables_even_when_pipeline_is_set():
    """Per-phase threshold=0.0 disables early-exit even when pipeline enables it."""
    ran = _run_loop(
        scores=[9.5, 9.5, 9.5],
        pipeline_threshold=8.5,  # would exit after round 1
        phase_threshold=0.0,     # phase disables it
    )
    assert len(ran) == 3


def test_phase_threshold_lower_than_pipeline_exits_sooner():
    """Phase threshold lower than pipeline triggers earlier exit."""
    ran = _run_loop(
        scores=[7.5, 9.0, 5.0],
        pipeline_threshold=8.5,  # exits at round 2 (9.0 >= 8.5)
        phase_threshold=7.0,     # exits at round 1 (7.5 >= 7.0)
    )
    assert ran == [7.5]


def test_phase_threshold_higher_than_pipeline_exits_later():
    """Phase threshold higher than pipeline means more rounds must run."""
    ran = _run_loop(
        scores=[8.5, 8.9, 9.5],
        pipeline_threshold=8.5,  # pipeline would exit after round 1
        phase_threshold=9.0,     # phase requires 9.0+ → round 3 (last)
    )
    # Round 3 (9.5 >= 9.0) triggers exit, but it's the last round → all 3 run.
    assert ran == [8.5, 8.9, 9.5]


def test_early_exit_with_two_inner_rounds():
    """Early exit with n_inner=2: exits after round 1 if score >= threshold."""
    ran = _run_loop(scores=[9.0, 5.0], pipeline_threshold=8.5)
    assert ran == [9.0]


def test_early_exit_single_inner_round_is_noop():
    """With only 1 inner round, early-exit is always a no-op."""
    ran = _run_loop(scores=[10.0], pipeline_threshold=8.5)
    assert ran == [10.0]


def test_best_score_tracked_correctly():
    """Early-exit uses the best score so far, not just the current score."""
    # Round 1: 9.0 (best=9.0, >= 8.5 threshold → would exit)
    ran = _run_loop(scores=[9.0, 4.0], pipeline_threshold=8.5)
    assert ran == [9.0]  # exits after round 1

    # Round 1: 4.0, Round 2: 9.0 (exits after round 2 since it's the last)
    ran2 = _run_loop(scores=[4.0, 9.0], pipeline_threshold=8.5)
    assert ran2 == [4.0, 9.0]  # round 2 is last, no skip
