"""Tests for harness/core/checkpoint.py

Covers CheckpointMetadata and CheckpointManager:
- Path-segment security validation
- Generic done/skipped markers (single and multi-segment)
- Phase, inner, synthesis, meta_review helpers
- Checkpoint metadata write/read round-trip
- Last-review hash write/read
- Missing-file safe reads
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.core.artifacts import ArtifactStore
from harness.core.checkpoint import CheckpointManager, CheckpointMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path, run_id: str = "test_run") -> ArtifactStore:
    return ArtifactStore(str(tmp_path), run_id)


def _make_ckpt(tmp_path: Path) -> CheckpointManager:
    return CheckpointManager(_make_store(tmp_path))


# ---------------------------------------------------------------------------
# CheckpointMetadata
# ---------------------------------------------------------------------------

class TestCheckpointMetadata:
    def test_fields_stored(self):
        meta = CheckpointMetadata(
            checkpoint_type="phase",
            outer_round=2,
            phase_label="1_testing",
            inner_index=3,
        )
        assert meta.checkpoint_type == "phase"
        assert meta.outer_round == 2
        assert meta.phase_label == "1_testing"
        assert meta.inner_index == 3

    def test_default_inner_index(self):
        meta = CheckpointMetadata(
            checkpoint_type="outer",
            outer_round=0,
            phase_label="",
            inner_index=0,
        )
        assert meta.inner_index == 0


# ---------------------------------------------------------------------------
# _validate_path_segments
# ---------------------------------------------------------------------------

class TestValidatePathSegments:
    def test_dotdot_raises_value_error(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        with pytest.raises(ValueError, match=r"'\.\.'"):
            ckpt._validate_path_segments("..", "other")

    def test_empty_segment_raises_value_error(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        with pytest.raises(ValueError, match="Empty path"):
            ckpt._validate_path_segments("")

    def test_valid_segment_no_error(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        # Should not raise
        ckpt._validate_path_segments("outer_0", "phase_analysis", "inner_2")

    def test_single_dot_allowed(self, tmp_path):
        # single dot is not blocked by the validator (only '..' is)
        ckpt = _make_ckpt(tmp_path)
        # Should not raise
        ckpt._validate_path_segments("some_key")


# ---------------------------------------------------------------------------
# Generic done / skipped markers
# ---------------------------------------------------------------------------

class TestDoneMarkers:
    def test_is_done_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_done("mykey") is False

    def test_mark_done_makes_is_done_true(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_done("mykey")
        assert ckpt.is_done("mykey") is True

    def test_multi_segment_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_done("outer_0", "phase_analysis", "inner_2")
        assert ckpt.is_done("outer_0", "phase_analysis", "inner_2") is True
        assert ckpt.is_done("outer_0", "phase_analysis") is False  # different key

    def test_different_keys_independent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_done("keyA")
        assert ckpt.is_done("keyA") is True
        assert ckpt.is_done("keyB") is False

    def test_mark_done_creates_file(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_done("mykey")
        # The marker should exist as a file somewhere in the run directory
        run_dir = ckpt.store.run_dir
        marker_files = list(Path(run_dir).rglob("*.done"))
        assert len(marker_files) >= 1

    def test_mark_done_idempotent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_done("mykey")
        ckpt.mark_done("mykey")  # No error on second call
        assert ckpt.is_done("mykey") is True


class TestSkippedMarkers:
    def test_is_skipped_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_skipped("key") is False

    def test_mark_skipped_makes_is_skipped_true(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_skipped("key")
        assert ckpt.is_skipped("key") is True

    def test_skipped_not_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_skipped("key")
        # is_done should not be True just because is_skipped is
        assert ckpt.is_done("key") is False


# ---------------------------------------------------------------------------
# Inner done markers
# ---------------------------------------------------------------------------

class TestInnerDoneMarkers:
    def test_is_inner_done_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_inner_done(0, "0_analysis", 0) is False

    def test_mark_inner_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_inner_done(0, "0_analysis", 1)
        assert ckpt.is_inner_done(0, "0_analysis", 1) is True

    def test_inner_done_different_index_independent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_inner_done(0, "0_analysis", 0)
        assert ckpt.is_inner_done(0, "0_analysis", 0) is True
        assert ckpt.is_inner_done(0, "0_analysis", 1) is False

    def test_inner_done_different_rounds_independent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_inner_done(0, "0_analysis", 0)
        assert ckpt.is_inner_done(1, "0_analysis", 0) is False


# ---------------------------------------------------------------------------
# Phase done markers
# ---------------------------------------------------------------------------

class TestPhaseDoneMarkers:
    def test_is_phase_done_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_phase_done(0, "0_analysis") is False

    def test_mark_phase_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_phase_done(0, "0_analysis")
        assert ckpt.is_phase_done(0, "0_analysis") is True

    def test_phase_done_different_rounds_independent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_phase_done(0, "0_analysis")
        assert ckpt.is_phase_done(1, "0_analysis") is False

    def test_phase_skipped_markers(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_phase_skipped(0, "0_analysis") is False
        ckpt.mark_phase_skipped(0, "0_analysis")
        assert ckpt.is_phase_skipped(0, "0_analysis") is True


# ---------------------------------------------------------------------------
# Synthesis done markers
# ---------------------------------------------------------------------------

class TestSynthesisDoneMarkers:
    def test_is_synthesis_done_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_synthesis_done(0, "0_analysis") is False

    def test_mark_synthesis_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_synthesis_done(0, "0_analysis")
        assert ckpt.is_synthesis_done(0, "0_analysis") is True


# ---------------------------------------------------------------------------
# Meta-review done markers
# ---------------------------------------------------------------------------

class TestMetaReviewDoneMarkers:
    def test_is_meta_review_done_initially_false(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.is_meta_review_done(0) is False

    def test_mark_meta_review_done(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_meta_review_done(0)
        assert ckpt.is_meta_review_done(0) is True

    def test_meta_review_different_rounds_independent(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.mark_meta_review_done(0)
        assert ckpt.is_meta_review_done(1) is False


# ---------------------------------------------------------------------------
# Checkpoint metadata write/read
# ---------------------------------------------------------------------------

class TestCheckpointMetadataWriteRead:
    def _make_meta(self) -> CheckpointMetadata:
        return CheckpointMetadata(
            checkpoint_type="phase",
            outer_round=3,
            phase_label="2_evaluation",
            inner_index=5,
        )

    def test_read_when_no_file_returns_none(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        assert ckpt.read_checkpoint_metadata() is None

    def test_write_then_read_round_trip(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        meta = self._make_meta()
        ckpt.write_checkpoint_metadata(meta)
        restored = ckpt.read_checkpoint_metadata()
        assert restored is not None
        assert restored.checkpoint_type == "phase"
        assert restored.outer_round == 3
        assert restored.phase_label == "2_evaluation"
        assert restored.inner_index == 5

    def test_overwrite_replaces_metadata(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        meta1 = CheckpointMetadata(
            checkpoint_type="outer", outer_round=1, phase_label="", inner_index=0
        )
        meta2 = CheckpointMetadata(
            checkpoint_type="phase", outer_round=2, phase_label="1_testing", inner_index=7
        )
        ckpt.write_checkpoint_metadata(meta1)
        ckpt.write_checkpoint_metadata(meta2)
        restored = ckpt.read_checkpoint_metadata()
        assert restored.outer_round == 2
        assert restored.phase_label == "1_testing"


# ---------------------------------------------------------------------------
# Last-review hash write/read
# ---------------------------------------------------------------------------

class TestLastReviewHash:
    def test_read_when_no_file_returns_empty_string(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        result = ckpt.read_last_review_hash()
        assert result == ""

    def test_write_then_read_returns_hash(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.write_last_review_hash("abc123def456")
        assert ckpt.read_last_review_hash() == "abc123def456"

    def test_overwrite_replaces_hash(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.write_last_review_hash("first_hash")
        ckpt.write_last_review_hash("second_hash")
        assert ckpt.read_last_review_hash() == "second_hash"

    def test_empty_hash_stored(self, tmp_path):
        ckpt = _make_ckpt(tmp_path)
        ckpt.write_last_review_hash("")
        assert ckpt.read_last_review_hash() == ""


# ---------------------------------------------------------------------------
# Store attribute
# ---------------------------------------------------------------------------

class TestCheckpointManagerStore:
    def test_store_is_accessible(self, tmp_path):
        store = _make_store(tmp_path)
        ckpt = CheckpointManager(store)
        assert ckpt.store is store
