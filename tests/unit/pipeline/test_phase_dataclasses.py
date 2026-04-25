"""Tests for PhaseConfig, ScoreItem, DualScore, InnerResult, PhaseResult in
harness/pipeline/phase.py."""

from __future__ import annotations

import pytest

from harness.pipeline.phase import (
    DualScore,
    InnerResult,
    PhaseConfig,
    PhaseResult,
    ScoreItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ds(score: float) -> DualScore:
    """Convenience: symmetric DualScore with given combined-equivalent score.
    combined = 0.6*basic + 0.4*diffusion, so equal basic/diffusion = score.
    """
    return DualScore(
        basic=ScoreItem(score=score, critique=""),
        diffusion=ScoreItem(score=score, critique=""),
    )


def _ir(round_num: int = 1, proposal: str = "p", score: float = 5.0) -> InnerResult:
    return InnerResult(proposal=proposal, dual_score=_ds(score))


def _phase_config(**kwargs) -> PhaseConfig:
    return PhaseConfig(name="t", index=0, system_prompt="sys", **kwargs)


# ---------------------------------------------------------------------------
# ScoreItem
# ---------------------------------------------------------------------------

class TestScoreItem:
    def test_construction(self):
        item = ScoreItem(score=0.7, critique="looks good")
        assert item.score == 0.7
        assert item.critique == "looks good"


# ---------------------------------------------------------------------------
# DualScore
# ---------------------------------------------------------------------------

class TestDualScore:
    def test_combined_symmetric(self):
        ds = DualScore(
            basic=ScoreItem(score=6.0, critique="c"),
            diffusion=ScoreItem(score=6.0, critique="a"),
        )
        # 0.6*6 + 0.4*6 = 6.0
        assert ds.combined == pytest.approx(6.0)

    def test_combined_weighted(self):
        ds = DualScore(
            basic=ScoreItem(score=8.0, critique=""),
            diffusion=ScoreItem(score=4.0, critique=""),
        )
        # 0.6*8 + 0.4*4 = 4.8 + 1.6 = 6.4
        assert ds.combined == pytest.approx(6.4)

    def test_combined_zeros(self):
        ds = DualScore(
            basic=ScoreItem(score=0.0, critique=""),
            diffusion=ScoreItem(score=0.0, critique=""),
        )
        assert ds.combined == pytest.approx(0.0)

    def test_combined_max(self):
        ds = DualScore(
            basic=ScoreItem(score=10.0, critique=""),
            diffusion=ScoreItem(score=10.0, critique=""),
        )
        assert ds.combined == pytest.approx(10.0)

    def test_out_of_range_raises(self):
        ds = DualScore(
            basic=ScoreItem(score=11.0, critique=""),
            diffusion=ScoreItem(score=5.0, critique=""),
        )
        with pytest.raises(ValueError, match="outside valid range"):
            _ = ds.combined


# ---------------------------------------------------------------------------
# InnerResult
# ---------------------------------------------------------------------------

class TestInnerResult:
    def test_combined_score_from_dual_score(self):
        ir = InnerResult(proposal="p", dual_score=_ds(7.0))
        assert ir.combined_score == pytest.approx(7.0)

    def test_combined_score_no_dual_score(self):
        ir = InnerResult(proposal="p", dual_score=None)
        assert ir.combined_score == 0.0

    def test_proposal_stored(self):
        ir = InnerResult(proposal="do the thing", dual_score=_ds(5.0))
        assert ir.proposal == "do the thing"

    def test_default_optional_fields(self):
        ir = InnerResult(proposal="x")
        assert ir.implement_log == ""
        assert ir.syntax_errors == ""
        assert ir.pytest_result == ""
        assert ir.tool_call_log == []

    def test_verdict_fallback_passed(self):
        class FakeVerdict:
            passed = True

        ir = InnerResult(proposal="p", dual_score=None, verdict=FakeVerdict())
        assert ir.combined_score == 10.0

    def test_verdict_fallback_failed(self):
        class FakeVerdict:
            passed = False

        ir = InnerResult(proposal="p", dual_score=None, verdict=FakeVerdict())
        assert ir.combined_score == 0.0


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

class TestPhaseResult:
    def _phase(self) -> PhaseConfig:
        return PhaseConfig(name="implementation", index=0, system_prompt="sp")

    def test_phase_and_synthesis(self):
        pr = PhaseResult(phase=self._phase(), synthesis="merged", best_score=7.5)
        assert pr.phase.name == "implementation"
        assert pr.synthesis == "merged"
        assert pr.best_score == pytest.approx(7.5)

    def test_inner_results_default_empty(self):
        pr = PhaseResult(phase=self._phase(), synthesis="", best_score=0.0)
        assert pr.inner_results == []

    def test_inner_results_stored(self):
        inners = [_ir(score=6.0), _ir(score=8.0)]
        pr = PhaseResult(phase=self._phase(), synthesis="s", best_score=8.0, inner_results=inners)
        assert len(pr.inner_results) == 2


# ---------------------------------------------------------------------------
# PhaseConfig.should_skip
# ---------------------------------------------------------------------------

class TestPhaseConfigShouldSkip:
    def test_no_skip_conditions(self):
        pc = _phase_config()
        assert not pc.should_skip(1)
        assert not pc.should_skip(100)

    def test_skip_after_round_boundary(self):
        pc = _phase_config(skip_after_round=3)
        assert not pc.should_skip(1)
        assert not pc.should_skip(3)
        assert pc.should_skip(4)

    def test_skip_cycle_every_other(self):
        pc = _phase_config(skip_cycle=2)
        # runs when outer % skip_cycle == 0
        assert not pc.should_skip(2)  # 2 % 2 == 0 → run
        assert not pc.should_skip(4)
        assert pc.should_skip(1)    # 1 % 2 != 0 → skip
        assert pc.should_skip(3)

    def test_skip_cycle_zero_raises_validation_error(self):
        # skip_cycle=0 is explicitly invalid (must be >= 1 or None)
        with pytest.raises(ValueError, match="skip_cycle"):
            _phase_config(skip_cycle=0)


# ---------------------------------------------------------------------------
# PhaseConfig.from_dict
# ---------------------------------------------------------------------------

class TestPhaseConfigFromDict:
    def test_basic_construction(self):
        data = {"name": "impl", "index": 0, "system_prompt": "sp"}
        pc = PhaseConfig.from_dict(data)
        assert pc.name == "impl"
        assert pc.index == 0

    def test_strips_comment_keys(self):
        data = {
            "name": "impl",
            "index": 0,
            "system_prompt": "sp",
            "//note": "ignored",
            "_hidden": "also ignored",
        }
        # should not raise; comment/private keys are stripped
        pc = PhaseConfig.from_dict(data)
        assert pc.name == "impl"

    def test_unknown_keys_raise(self):
        data = {
            "name": "impl",
            "index": 0,
            "system_prompt": "sp",
            "nonexistent_field": True,
        }
        with pytest.raises(TypeError):
            PhaseConfig.from_dict(data)


# ---------------------------------------------------------------------------
# PhaseConfig.label
# ---------------------------------------------------------------------------

class TestPhaseConfigLabel:
    def test_label_is_string(self):
        pc = PhaseConfig(name="foo", index=0, system_prompt="s")
        assert isinstance(pc.label, str)
        assert len(pc.label) > 0

    def test_label_contains_name_or_index(self):
        pc = PhaseConfig(name="bar", index=3, system_prompt="s")
        assert "bar" in pc.label or "3" in pc.label
