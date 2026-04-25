"""Tests for phase.py data classes — PhaseConfig, ScoreItem, DualScore, InnerResult, PhaseResult."""
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
# ScoreItem
# ---------------------------------------------------------------------------


class TestScoreItem:
    def test_basic_construction(self) -> None:
        item = ScoreItem(score=7.0, critique="well done")
        assert item.score == 7.0
        assert item.critique == "well done"

    def test_zero_score(self) -> None:
        item = ScoreItem(score=0.0, critique="fail")
        assert item.score == 0.0

    def test_ten_score(self) -> None:
        item = ScoreItem(score=10.0, critique="perfect")
        assert item.score == 10.0

    def test_fractional_score(self) -> None:
        item = ScoreItem(score=6.5, critique="ok")
        assert item.score == 6.5

    def test_empty_critique(self) -> None:
        item = ScoreItem(score=5.0, critique="")
        assert item.critique == ""


# ---------------------------------------------------------------------------
# DualScore.combined
# ---------------------------------------------------------------------------


class TestDualScoreCombined:
    """DualScore.combined = 0.6 * basic + 0.4 * diffusion."""

    def _make(self, basic: float, diffusion: float) -> DualScore:
        return DualScore(
            basic=ScoreItem(score=basic, critique=""),
            diffusion=ScoreItem(score=diffusion, critique=""),
        )

    def test_equal_scores(self) -> None:
        ds = self._make(8.0, 8.0)
        assert ds.combined == pytest.approx(8.0)

    def test_weighted_split(self) -> None:
        # 60% of 10, 40% of 5 = 6 + 2 = 8
        ds = self._make(10.0, 5.0)
        assert ds.combined == pytest.approx(8.0)

    def test_weighted_split_reversed(self) -> None:
        # 60% of 5, 40% of 10 = 3 + 4 = 7
        ds = self._make(5.0, 10.0)
        assert ds.combined == pytest.approx(7.0)

    def test_zero_both(self) -> None:
        ds = self._make(0.0, 0.0)
        assert ds.combined == pytest.approx(0.0)

    def test_ten_both(self) -> None:
        ds = self._make(10.0, 10.0)
        assert ds.combined == pytest.approx(10.0)

    def test_boundary_zero_basic(self) -> None:
        ds = self._make(0.0, 10.0)
        # 0.6*0 + 0.4*10 = 4.0
        assert ds.combined == pytest.approx(4.0)

    def test_boundary_zero_diffusion(self) -> None:
        ds = self._make(10.0, 0.0)
        # 0.6*10 + 0.4*0 = 6.0
        assert ds.combined == pytest.approx(6.0)

    def test_fractional_scores(self) -> None:
        ds = self._make(7.5, 6.5)
        # 0.6*7.5 + 0.4*6.5 = 4.5 + 2.6 = 7.1
        assert ds.combined == pytest.approx(7.1)

    def test_basic_score_out_of_range_high_raises(self) -> None:
        ds = self._make(11.0, 5.0)
        with pytest.raises(ValueError):
            _ = ds.combined

    def test_basic_score_out_of_range_low_raises(self) -> None:
        ds = self._make(-0.1, 5.0)
        with pytest.raises(ValueError):
            _ = ds.combined

    def test_diffusion_score_out_of_range_high_raises(self) -> None:
        ds = self._make(5.0, 10.1)
        with pytest.raises(ValueError):
            _ = ds.combined

    def test_diffusion_score_out_of_range_low_raises(self) -> None:
        ds = self._make(5.0, -1.0)
        with pytest.raises(ValueError):
            _ = ds.combined

    def test_result_does_not_exceed_10(self) -> None:
        ds = self._make(10.0, 10.0)
        assert ds.combined <= 10.0

    def test_result_does_not_go_below_0(self) -> None:
        ds = self._make(0.0, 0.0)
        assert ds.combined >= 0.0


# ---------------------------------------------------------------------------
# InnerResult.combined_score
# ---------------------------------------------------------------------------


class TestInnerResultCombinedScore:
    """InnerResult.combined_score uses dual_score when present, else verdict."""

    def _make_dual(self, basic: float, diffusion: float) -> InnerResult:
        return InnerResult(
            proposal="some proposal",
            dual_score=DualScore(
                basic=ScoreItem(score=basic, critique=""),
                diffusion=ScoreItem(score=diffusion, critique=""),
            ),
        )

    def test_dual_score_used_when_present(self) -> None:
        r = self._make_dual(8.0, 6.0)
        # 0.6*8 + 0.4*6 = 4.8 + 2.4 = 7.2
        assert r.combined_score == pytest.approx(7.2)

    def test_dual_score_perfect(self) -> None:
        r = self._make_dual(10.0, 10.0)
        assert r.combined_score == pytest.approx(10.0)

    def test_dual_score_zero(self) -> None:
        r = self._make_dual(0.0, 0.0)
        assert r.combined_score == pytest.approx(0.0)

    def test_no_dual_score_no_verdict_returns_zero(self) -> None:
        r = InnerResult(proposal="x")
        assert r.combined_score == 0.0

    def test_verdict_passed_true_returns_10(self) -> None:
        class FakeVerdict:
            passed = True

        r = InnerResult(proposal="x", verdict=FakeVerdict())
        assert r.combined_score == 10.0

    def test_verdict_passed_false_returns_zero(self) -> None:
        class FakeVerdict:
            passed = False

        r = InnerResult(proposal="x", verdict=FakeVerdict())
        assert r.combined_score == 0.0

    def test_dual_score_takes_priority_over_verdict(self) -> None:
        """When both dual_score and verdict are set, dual_score wins."""

        class FakeVerdict:
            passed = True

        r = InnerResult(
            proposal="x",
            dual_score=DualScore(
                basic=ScoreItem(score=5.0, critique=""),
                diffusion=ScoreItem(score=5.0, critique=""),
            ),
            verdict=FakeVerdict(),
        )
        # dual_score gives 5.0, verdict would give 10.0 — dual_score wins
        assert r.combined_score == pytest.approx(5.0)

    def test_default_fields(self) -> None:
        r = InnerResult(proposal="hello")
        assert r.implement_log == ""
        assert r.verdict is None
        assert r.dual_score is None


# ---------------------------------------------------------------------------
# PhaseConfig.label
# ---------------------------------------------------------------------------


class TestPhaseConfigLabel:
    def _make(self, name: str, index: int) -> PhaseConfig:
        return PhaseConfig(name=name, index=index, system_prompt="s")

    def test_label_zero_index(self) -> None:
        assert self._make("analysis", 0).label == "1_analysis"

    def test_label_nonzero_index(self) -> None:
        assert self._make("implement", 2).label == "3_implement"

    def test_label_with_underscores_in_name(self) -> None:
        assert self._make("code_review", 4).label == "5_code_review"


# ---------------------------------------------------------------------------
# PhaseConfig.__post_init__ — skip_cycle validation
# ---------------------------------------------------------------------------


class TestPhaseConfigPostInit:
    def test_valid_skip_cycle(self) -> None:
        # Should not raise
        cfg = PhaseConfig(name="x", index=0, system_prompt="s", skip_cycle=3)
        assert cfg.skip_cycle == 3

    def test_skip_cycle_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            PhaseConfig(name="x", index=0, system_prompt="s", skip_cycle=0)

    def test_skip_cycle_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            PhaseConfig(name="x", index=0, system_prompt="s", skip_cycle=-1)

    def test_skip_cycle_none_ok(self) -> None:
        cfg = PhaseConfig(name="x", index=0, system_prompt="s", skip_cycle=None)
        assert cfg.skip_cycle is None


# ---------------------------------------------------------------------------
# PhaseConfig._validate_allowed_edit_globs
# ---------------------------------------------------------------------------


class TestValidateAllowedEditGlobs:
    def _make(self, globs: list[str]) -> PhaseConfig:
        return PhaseConfig(
            name="x", index=0, system_prompt="s", allowed_edit_globs=globs
        )

    def test_empty_list_ok(self) -> None:
        cfg = self._make([])
        assert cfg.allowed_edit_globs == []

    def test_valid_relative_glob(self) -> None:
        cfg = self._make(["harness/**/*.py"])
        assert len(cfg.allowed_edit_globs) == 1

    def test_dotdot_in_glob_raises(self) -> None:
        with pytest.raises(ValueError):
            self._make(["../secret/**"])

    def test_dotdot_deep_raises(self) -> None:
        with pytest.raises(ValueError):
            self._make(["harness/../../etc/passwd"])

    def test_absolute_path_raises(self) -> None:
        with pytest.raises(ValueError):
            self._make(["/etc/passwd"])

    def test_multiple_valid_globs(self) -> None:
        cfg = self._make(["harness/**/*.py", "tests/*.py", "*.md"])
        assert len(cfg.allowed_edit_globs) == 3


# ---------------------------------------------------------------------------
# PhaseConfig.should_skip
# ---------------------------------------------------------------------------


class TestPhaseConfigShouldSkip:
    def _make(
        self,
        skip_after_round: int | None = None,
        skip_cycle: int | None = None,
    ) -> PhaseConfig:
        return PhaseConfig(
            name="x",
            index=0,
            system_prompt="s",
            skip_after_round=skip_after_round,
            skip_cycle=skip_cycle,
        )

    # --- no skip conditions set ---

    def test_no_conditions_never_skips(self) -> None:
        cfg = self._make()
        for outer in range(10):
            assert not cfg.should_skip(outer)

    # --- skip_after_round ---

    def test_skip_after_round_not_triggered_on_equal(self) -> None:
        cfg = self._make(skip_after_round=2)
        assert not cfg.should_skip(2)

    def test_skip_after_round_not_triggered_below(self) -> None:
        cfg = self._make(skip_after_round=2)
        assert not cfg.should_skip(1)
        assert not cfg.should_skip(0)

    def test_skip_after_round_triggered_above(self) -> None:
        cfg = self._make(skip_after_round=2)
        assert cfg.should_skip(3)
        assert cfg.should_skip(100)

    def test_skip_after_round_zero(self) -> None:
        # skip after round 0 means skip from round 1 onwards
        cfg = self._make(skip_after_round=0)
        assert not cfg.should_skip(0)
        assert cfg.should_skip(1)

    # --- skip_cycle ---

    def test_skip_cycle_runs_on_multiples(self) -> None:
        cfg = self._make(skip_cycle=3)
        assert not cfg.should_skip(0)
        assert not cfg.should_skip(3)
        assert not cfg.should_skip(6)

    def test_skip_cycle_skips_non_multiples(self) -> None:
        cfg = self._make(skip_cycle=3)
        assert cfg.should_skip(1)
        assert cfg.should_skip(2)
        assert cfg.should_skip(4)
        assert cfg.should_skip(5)

    def test_skip_cycle_1_never_skips(self) -> None:
        # cycle=1: outer % 1 == 0 always, so never skips
        cfg = self._make(skip_cycle=1)
        for outer in range(10):
            assert not cfg.should_skip(outer)

    def test_skip_cycle_2_alternates(self) -> None:
        cfg = self._make(skip_cycle=2)
        assert not cfg.should_skip(0)
        assert cfg.should_skip(1)
        assert not cfg.should_skip(2)
        assert cfg.should_skip(3)

    # --- both conditions ---

    def test_either_condition_triggers_skip(self) -> None:
        # skip_after_round=5, skip_cycle=2 — skip if outer>5 OR outer%2!=0
        cfg = self._make(skip_after_round=5, skip_cycle=2)
        assert not cfg.should_skip(0)   # 0<=5, 0%2==0
        assert cfg.should_skip(1)       # 1%2!=0 triggers
        assert not cfg.should_skip(2)   # 2<=5, 2%2==0
        assert cfg.should_skip(7)       # 7>5 triggers


# ---------------------------------------------------------------------------
# PhaseConfig.from_dict
# ---------------------------------------------------------------------------


class TestPhaseConfigFromDict:
    def test_minimal_dict(self) -> None:
        cfg = PhaseConfig.from_dict(
            {"name": "test", "index": 0, "system_prompt": "hello"}
        )
        assert cfg.name == "test"
        assert cfg.index == 0
        assert cfg.system_prompt == "hello"

    def test_strips_comment_keys(self) -> None:
        cfg = PhaseConfig.from_dict(
            {
                "name": "test",
                "index": 0,
                "system_prompt": "s",
                "// comment": "this should be stripped",
            }
        )
        assert cfg.name == "test"

    def test_strips_underscore_keys(self) -> None:
        cfg = PhaseConfig.from_dict(
            {
                "name": "test",
                "index": 0,
                "system_prompt": "s",
                "_disabled_feature": True,
            }
        )
        assert cfg.name == "test"

    def test_full_config_dict(self) -> None:
        cfg = PhaseConfig.from_dict(
            {
                "name": "implement",
                "index": 1,
                "system_prompt": "Build the thing",
                "mode": "implement",
                "run_tests": True,
                "test_path": "tests/",
                "glob_patterns": ["harness/**/*.py"],
                "skip_after_round": 3,
                "skip_cycle": 2,
                "inner_rounds": 5,
                "min_proposal_chars": 100,
                "commit_on_success": True,
            }
        )
        assert cfg.name == "implement"
        assert cfg.mode == "implement"
        assert cfg.run_tests is True
        assert cfg.skip_after_round == 3
        assert cfg.skip_cycle == 2
        assert cfg.inner_rounds == 5
        assert cfg.min_proposal_chars == 100
        assert cfg.commit_on_success is True

    def test_mode_debate_default(self) -> None:
        cfg = PhaseConfig.from_dict(
            {"name": "analysis", "index": 0, "system_prompt": "s"}
        )
        assert cfg.mode == "debate"

    def test_falsifiable_criterion_set(self) -> None:
        cfg = PhaseConfig.from_dict(
            {
                "name": "test",
                "index": 0,
                "system_prompt": "s",
                "falsifiable_criterion": "All tests pass",
            }
        )
        assert cfg.falsifiable_criterion == "All tests pass"

    def test_invalid_skip_cycle_raises(self) -> None:
        with pytest.raises(ValueError):
            PhaseConfig.from_dict(
                {
                    "name": "test",
                    "index": 0,
                    "system_prompt": "s",
                    "skip_cycle": 0,
                }
            )

    def test_allowed_edit_globs_validated(self) -> None:
        with pytest.raises(ValueError):
            PhaseConfig.from_dict(
                {
                    "name": "test",
                    "index": 0,
                    "system_prompt": "s",
                    "allowed_edit_globs": ["../outside/**"],
                }
            )


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------


class TestPhaseResult:
    def _make_phase(self) -> PhaseConfig:
        return PhaseConfig(name="analysis", index=0, system_prompt="s")

    def test_basic_construction(self) -> None:
        phase = self._make_phase()
        result = PhaseResult(
            phase=phase, synthesis="summary", best_score=7.5
        )
        assert result.synthesis == "summary"
        assert result.best_score == 7.5
        assert result.inner_results == []

    def test_with_inner_results(self) -> None:
        phase = self._make_phase()
        inner = InnerResult(proposal="proposal text")
        result = PhaseResult(
            phase=phase,
            synthesis="summary",
            best_score=8.0,
            inner_results=[inner],
        )
        assert len(result.inner_results) == 1
        assert result.inner_results[0].proposal == "proposal text"

    def test_zero_best_score(self) -> None:
        result = PhaseResult(
            phase=self._make_phase(), synthesis="", best_score=0.0
        )
        assert result.best_score == 0.0

    def test_perfect_best_score(self) -> None:
        result = PhaseResult(
            phase=self._make_phase(), synthesis="", best_score=10.0
        )
        assert result.best_score == 10.0
