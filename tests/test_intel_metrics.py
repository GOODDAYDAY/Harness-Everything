"""Tests for harness/pipeline/intel_metrics.py."""
from __future__ import annotations

import json
import pathlib

import pytest

from harness.pipeline.intel_metrics import (
    _extract_rho,
    _rank,
    format_trajectory,
    spearman_rho,
)


# ---------------------------------------------------------------------------
# _rank
# ---------------------------------------------------------------------------


class TestRank:
    def test_empty(self):
        assert _rank([]) == []

    def test_single(self):
        assert _rank([42.0]) == [1.0]

    def test_distinct_ascending(self):
        # [1, 2, 3] → ranks 1, 2, 3
        assert _rank([1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]

    def test_distinct_descending(self):
        # [3, 2, 1] → ranks 3, 2, 1
        assert _rank([3.0, 2.0, 1.0]) == [3.0, 2.0, 1.0]

    def test_two_equal(self):
        # [1, 1] → average of ranks 1,2 = 1.5, 1.5
        assert _rank([1.0, 1.0]) == [1.5, 1.5]

    def test_three_equal(self):
        # all equal → average rank = 2.0
        assert _rank([5.0, 5.0, 5.0]) == [2.0, 2.0, 2.0]

    def test_tie_at_start(self):
        # [1, 1, 3] → [1.5, 1.5, 3]
        r = _rank([1.0, 1.0, 3.0])
        assert r[0] == 1.5
        assert r[1] == 1.5
        assert r[2] == 3.0

    def test_tie_at_end(self):
        # [1, 3, 3] → [1, 2.5, 2.5]
        r = _rank([1.0, 3.0, 3.0])
        assert r[0] == 1.0
        assert r[1] == 2.5
        assert r[2] == 2.5

    def test_tie_in_middle(self):
        # [1, 2, 2, 4] → [1, 2.5, 2.5, 4]
        r = _rank([1.0, 2.0, 2.0, 4.0])
        assert r[0] == 1.0
        assert r[1] == 2.5
        assert r[2] == 2.5
        assert r[3] == 4.0

    def test_preserves_original_order(self):
        # ranks should be indexed by *original* position, not sorted position
        r = _rank([30.0, 10.0, 20.0])
        assert r[0] == 3.0  # 30 is the largest → rank 3
        assert r[1] == 1.0  # 10 is the smallest → rank 1
        assert r[2] == 2.0  # 20 is the middle → rank 2


# ---------------------------------------------------------------------------
# spearman_rho
# ---------------------------------------------------------------------------


class TestSpearmanRho:
    def test_perfect_positive_correlation(self):
        rho = spearman_rho([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
        assert rho is not None
        assert abs(rho - 1.0) < 1e-10

    def test_perfect_negative_correlation(self):
        rho = spearman_rho([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
        assert rho is not None
        assert abs(rho - (-1.0)) < 1e-10

    def test_no_correlation_constant(self):
        # constant y → no correlation → None
        rho = spearman_rho([1, 2, 3], [5, 5, 5])
        assert rho is None

    def test_no_correlation_constant_x(self):
        rho = spearman_rho([7, 7, 7], [1, 2, 3])
        assert rho is None

    def test_too_short(self):
        # need at least 2 points
        assert spearman_rho([1], [2]) is None

    def test_length_mismatch(self):
        assert spearman_rho([1, 2], [1, 2, 3]) is None

    def test_empty(self):
        assert spearman_rho([], []) is None

    def test_known_value(self):
        # Verified with: scipy.stats.spearmanr([1,2,3,4,5], [5,6,7,8,7])
        # → 0.8207826816681233
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [5.0, 6.0, 7.0, 8.0, 7.0]
        rho = spearman_rho(x, y)
        assert rho is not None
        assert abs(rho - 0.8207826816681233) < 0.001

    def test_rho_bounds(self):
        import random
        rng = random.Random(42)
        x = [rng.random() for _ in range(20)]
        y = [rng.random() for _ in range(20)]
        rho = spearman_rho(x, y)
        assert rho is not None
        assert -1.0 <= rho <= 1.0

    def test_monotone_transform(self):
        """A monotone transform of x should keep |rho| = 1."""
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [v ** 3 for v in x]  # monotone increasing transform
        rho = spearman_rho(x, y)
        assert rho is not None
        assert abs(rho - 1.0) < 1e-10

    def test_ties_handled(self):
        """Values with ties should still return a valid rho."""
        x = [1.0, 1.0, 2.0, 3.0]
        y = [1.0, 2.0, 3.0, 4.0]
        rho = spearman_rho(x, y)
        assert rho is not None
        assert -1.0 <= rho <= 1.0


# ---------------------------------------------------------------------------
# _extract_rho
# ---------------------------------------------------------------------------


class TestExtractRho:
    def test_rho_key_present(self):
        assert _extract_rho({"rho": 0.87}) == pytest.approx(0.87)

    def test_rho_key_int(self):
        assert _extract_rho({"rho": 1}) == pytest.approx(1.0)

    def test_rho_basic_only(self):
        assert _extract_rho({"rho_basic": 0.8}) == pytest.approx(0.8)

    def test_rho_diffusion_only(self):
        assert _extract_rho({"rho_diffusion": 0.9}) == pytest.approx(0.9)

    def test_both_subs_averaged(self):
        result = _extract_rho({"rho_basic": 0.8, "rho_diffusion": 0.9})
        assert result == pytest.approx(0.85)

    def test_rho_preferred_over_subs(self):
        # explicit 'rho' key wins over sub-scores
        result = _extract_rho({"rho": 0.7, "rho_basic": 0.8, "rho_diffusion": 0.9})
        assert result == pytest.approx(0.7)

    def test_missing_all(self):
        assert _extract_rho({"n": 10, "elapsed_s": 5.0}) is None

    def test_non_numeric_rho(self):
        assert _extract_rho({"rho": "high"}) is None

    def test_none_rho(self):
        assert _extract_rho({"rho": None}) is None

    def test_empty_dict(self):
        assert _extract_rho({}) is None


# ---------------------------------------------------------------------------
# format_trajectory
# ---------------------------------------------------------------------------


class TestFormatTrajectoryMissingFile:
    def test_missing_file_returns_empty(self, tmp_path: pathlib.Path):
        result = format_trajectory(str(tmp_path / "nonexistent.jsonl"))
        assert result["current"] is None
        assert result["delta"] is None
        assert result["trajectory"] == []
        assert result["regressions_in_last_5"] == 0

    def test_empty_file_returns_empty(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text("", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] is None

    def test_blank_lines_ignored(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text("\n   \n\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] is None

    def test_invalid_json_lines_skipped(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(
            'not json\n{"rho": 0.8}\nalso not json\n',
            encoding="utf-8",
        )
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.8)


class TestFormatTrajectorySingleRow:
    def test_single_row_no_delta(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(json.dumps({"rho": 0.75}) + "\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.75)
        assert result["delta"] is None
        assert result["trajectory"] == pytest.approx([0.75])
        assert result["regressions_in_last_5"] == 0

    def test_uses_rho_basic_fallback(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(json.dumps({"rho_basic": 0.6, "n": 20}) + "\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.6)

    def test_uses_sub_average_fallback(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(
            json.dumps({"rho_basic": 0.7, "rho_diffusion": 0.9}) + "\n",
            encoding="utf-8",
        )
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.8)

    def test_row_without_rho_skipped(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(json.dumps({"n": 10, "elapsed_s": 5}) + "\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] is None


class TestFormatTrajectoryMultipleRows:
    def _write(self, path: pathlib.Path, rhos: list[float]) -> None:
        lines = [json.dumps({"rho": r}) for r in rhos]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_delta_positive(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        self._write(p, [0.7, 0.8])
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.8)
        assert result["delta"] == pytest.approx(0.1, abs=1e-9)

    def test_delta_negative(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        self._write(p, [0.85, 0.75])
        result = format_trajectory(str(p))
        assert result["delta"] == pytest.approx(-0.1, abs=1e-9)

    def test_trajectory_capped_at_20(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        rhos = [round(i * 0.01, 4) for i in range(1, 31)]  # 30 values
        self._write(p, rhos)
        result = format_trajectory(str(p))
        assert len(result["trajectory"]) == 20
        # Should be the most recent 20
        assert result["trajectory"][0] == pytest.approx(rhos[10])
        assert result["trajectory"][-1] == pytest.approx(rhos[-1])

    def test_trajectory_oldest_first(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        self._write(p, [0.1, 0.2, 0.3])
        result = format_trajectory(str(p))
        assert result["trajectory"] == pytest.approx([0.1, 0.2, 0.3])

    def test_no_regressions_when_always_increasing(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        self._write(p, [0.6, 0.7, 0.8, 0.85, 0.9])
        result = format_trajectory(str(p))
        assert result["regressions_in_last_5"] == 0

    def test_regressions_counted_correctly(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        # last-5 values (oldest first): 0.9, 0.8, 0.85, 0.75, 0.8
        # regressions: 0.9→0.8 (↓), 0.85→0.75 (↓) → 2 regressions
        self._write(p, [0.9, 0.8, 0.85, 0.75, 0.8])
        result = format_trajectory(str(p))
        assert result["regressions_in_last_5"] == 2

    def test_regressions_uses_last_6_points(self, tmp_path: pathlib.Path):
        """last-5 pairs means we look at up to 6 values."""
        p = tmp_path / "probe.jsonl"
        # 10 values, last 6 = [0.5, 0.6, 0.7, 0.6, 0.65, 0.7]
        # pairs: 0.5→0.6(+), 0.6→0.7(+), 0.7→0.6(-), 0.6→0.65(+), 0.65→0.7(+)
        # → 1 regression
        early = [0.9, 0.8, 0.85, 0.95]  # 4 early, irrelevant
        late = [0.5, 0.6, 0.7, 0.6, 0.65, 0.7]  # 6 late
        self._write(p, early + late)
        result = format_trajectory(str(p))
        assert result["regressions_in_last_5"] == 1

    def test_all_regressions(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        # monotone decreasing last 5
        self._write(p, [0.9, 0.8, 0.7, 0.6, 0.5])
        result = format_trajectory(str(p))
        assert result["regressions_in_last_5"] == 4

    def test_current_is_last_value(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        self._write(p, [0.5, 0.6, 0.9])
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.9)

    def test_rows_without_rho_mixed_in(self, tmp_path: pathlib.Path):
        """Rows without any rho key should be silently skipped."""
        p = tmp_path / "probe.jsonl"
        lines = [
            json.dumps({"rho": 0.7}),
            json.dumps({"n": 10, "elapsed_s": 5}),  # no rho → skipped
            json.dumps({"rho": 0.8}),
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["trajectory"] == pytest.approx([0.7, 0.8])
        assert result["current"] == pytest.approx(0.8)


class TestFormatTrajectoryEdgeCases:
    def test_two_rows_delta_zero(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        p.write_text(
            json.dumps({"rho": 0.8}) + "\n" + json.dumps({"rho": 0.8}) + "\n",
            encoding="utf-8",
        )
        result = format_trajectory(str(p))
        assert result["delta"] == pytest.approx(0.0)
        assert result["regressions_in_last_5"] == 0

    def test_unicode_in_file_tolerated(self, tmp_path: pathlib.Path):
        p = tmp_path / "probe.jsonl"
        # Extra unicode fields should not break parsing
        p.write_text(
            json.dumps({"rho": 0.85, "note": "Spearman \u03c1 improved"}) + "\n",
            encoding="utf-8",
        )
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(0.85)

    def test_fractional_rho_precision(self, tmp_path: pathlib.Path):
        """Ensure floating-point values survive the round-trip."""
        p = tmp_path / "probe.jsonl"
        rho_val = 0.8732819048
        p.write_text(json.dumps({"rho": rho_val}) + "\n", encoding="utf-8")
        result = format_trajectory(str(p))
        assert result["current"] == pytest.approx(rho_val, rel=1e-6)


# ---------------------------------------------------------------------------
# Calibration benchmark structure tests
# ---------------------------------------------------------------------------

_BENCHMARKS_DIR = pathlib.Path(__file__).parent.parent / "benchmarks"
_GT_PATH = _BENCHMARKS_DIR / "evaluator_calibration" / "ground_truth.json"
_PROPOSALS_DIR = _BENCHMARKS_DIR / "evaluator_calibration" / "proposals"


class TestCalibrationBenchmarkStructure:
    """Validate that the calibration benchmark files are consistent and complete."""

    def test_ground_truth_file_exists(self):
        assert _GT_PATH.exists(), f"ground_truth.json not found at {_GT_PATH}"

    def test_ground_truth_is_valid_json(self):
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert len(data) >= 3, "Need at least 3 proposals for meaningful Spearman rho"

    def test_ground_truth_scores_in_range(self):
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        for name, score in data.items():
            assert isinstance(score, (int, float)), f"{name}: score must be numeric"
            assert 0.0 <= score <= 10.0, f"{name}: score {score} out of 0-10 range"

    def test_ground_truth_scores_are_discriminating(self):
        """Scores must span at least 5 points to be useful for rho measurement."""
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        scores = list(data.values())
        assert max(scores) - min(scores) >= 5.0, (
            f"Score range {min(scores)}-{max(scores)} is too narrow for calibration"
        )

    def test_proposals_dir_exists(self):
        assert _PROPOSALS_DIR.exists(), f"proposals/ not found at {_PROPOSALS_DIR}"

    def test_each_gt_entry_has_proposal_file(self):
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        for name in data:
            pf = _PROPOSALS_DIR / name / "proposal.md"
            assert pf.exists(), f"Missing proposal.md for '{name}' at {pf}"

    def test_proposal_files_are_non_empty(self):
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        for name in data:
            pf = _PROPOSALS_DIR / name / "proposal.md"
            if pf.exists():
                content = pf.read_text(encoding="utf-8")
                assert len(content) >= 50, f"Proposal '{name}' is suspiciously short"

    def test_load_proposals_returns_all_entries(self):
        from harness.pipeline.intel_metrics import _load_ground_truth, _load_proposals

        gt = _load_ground_truth(_GT_PATH)
        proposals = _load_proposals(_PROPOSALS_DIR, gt)
        assert len(proposals) == len(gt), (
            f"Expected {len(gt)} proposals, got {len(proposals)}"
        )

    def test_load_proposals_text_matches_file_content(self):
        from harness.pipeline.intel_metrics import _load_ground_truth, _load_proposals

        gt = _load_ground_truth(_GT_PATH)
        proposals = _load_proposals(_PROPOSALS_DIR, gt)
        for name, entry in proposals.items():
            text = entry["text"]
            expected_score = entry["gt"]
            on_disk = (_PROPOSALS_DIR / name / "proposal.md").read_text(encoding="utf-8")
            assert text == on_disk, f"Proposal text for '{name}' does not match file"
            assert expected_score == gt[name]

    def test_quality_ordering_is_strict(self):
        """The top 2 and bottom 2 GT scores must not overlap — enforces clear discrimination."""
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        scores = sorted(data.values())
        # Top 2 should all be > bottom 2
        assert scores[-2] > scores[1], (
            "Second-best score must exceed second-worst: "
            f"second-best={scores[-2]}, second-worst={scores[1]}"
        )

    def test_no_duplicate_scores(self):
        """All GT scores must be distinct (tied scores make Spearman rho unreliable)."""
        data = json.loads(_GT_PATH.read_text(encoding="utf-8"))
        scores = list(data.values())
        assert len(scores) == len(set(scores)), (
            f"Duplicate GT scores found: {scores}"
        )


class TestLoadGroundTruth:
    """Unit tests for _load_ground_truth error handling."""

    def test_raises_on_non_dict_json(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_ground_truth

        p = tmp_path / "gt.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a JSON object"):
            _load_ground_truth(p)

    def test_converts_int_scores_to_float(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_ground_truth

        p = tmp_path / "gt.json"
        p.write_text('{"a": 7, "b": 3}', encoding="utf-8")
        result = _load_ground_truth(p)
        assert result == {"a": 7.0, "b": 3.0}
        assert all(isinstance(v, float) for v in result.values())

    def test_converts_string_keys_to_str(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_ground_truth

        p = tmp_path / "gt.json"
        p.write_text('{"proposal1": 8.0}', encoding="utf-8")
        result = _load_ground_truth(p)
        assert "proposal1" in result


class TestLoadProposals:
    """Unit tests for _load_proposals."""

    def test_skips_proposals_without_gt_entry(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_proposals

        # Create two proposal dirs but only one has a GT entry
        (tmp_path / "proposal_a").mkdir()
        (tmp_path / "proposal_a" / "proposal.md").write_text("Content A", encoding="utf-8")
        (tmp_path / "proposal_b").mkdir()
        (tmp_path / "proposal_b" / "proposal.md").write_text("Content B", encoding="utf-8")

        gt = {"proposal_a": 8.0}  # proposal_b is NOT in gt
        result = _load_proposals(tmp_path, gt)

        assert "proposal_a" in result
        assert "proposal_b" not in result

    def test_skips_dirs_without_proposal_md(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_proposals

        # Create a dir with a GT entry but no proposal.md
        (tmp_path / "proposal_c").mkdir()
        gt = {"proposal_c": 5.0}
        result = _load_proposals(tmp_path, gt)
        assert "proposal_c" not in result

    def test_loads_text_and_score_correctly(self, tmp_path: pathlib.Path):
        from harness.pipeline.intel_metrics import _load_proposals

        (tmp_path / "my_proposal").mkdir()
        (tmp_path / "my_proposal" / "proposal.md").write_text(
            "## Summary\nDid the work.", encoding="utf-8"
        )
        gt = {"my_proposal": 7.5}
        result = _load_proposals(tmp_path, gt)

        assert "my_proposal" in result
        assert result["my_proposal"]["text"] == "## Summary\nDid the work."
        assert result["my_proposal"]["gt"] == 7.5
