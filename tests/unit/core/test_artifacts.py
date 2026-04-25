"""Unit tests for harness.core.artifacts.ArtifactStore."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.core.artifacts import ArtifactStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(base_dir=tmp_path, run_id="test-run-001")


# ---------------------------------------------------------------------------
# __init__ and path()
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_run_directory(self, tmp_path: Path) -> None:
        ArtifactStore(base_dir=tmp_path, run_id="my-run")
        assert (tmp_path / "my-run").is_dir()

    def test_auto_run_id_when_none(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path, run_id=None)
        # run_dir is named run_YYYYMMDDTHHMMSS
        assert store.run_dir.name.startswith("run_")
        assert store.run_dir.is_dir()

    def test_path_single_segment(self, store: ArtifactStore, tmp_path: Path) -> None:
        p = store.path("foo.txt")
        assert p == tmp_path / "test-run-001" / "foo.txt"

    def test_path_multiple_segments(self, store: ArtifactStore, tmp_path: Path) -> None:
        p = store.path("round_1", "phase_1_dev", "inner_1", "proposal.txt")
        expected = tmp_path / "test-run-001" / "round_1" / "phase_1_dev" / "inner_1" / "proposal.txt"
        assert p == expected

    def test_path_returns_path_object(self, store: ArtifactStore) -> None:
        assert isinstance(store.path("bar.json"), Path)

    def test_different_run_ids_stay_isolated(self, tmp_path: Path) -> None:
        store_a = ArtifactStore(base_dir=tmp_path, run_id="run-a")
        store_b = ArtifactStore(base_dir=tmp_path, run_id="run-b")
        store_a.write("hello", "data.txt")
        assert not store_b.exists("data.txt")

    def test_str_base_dir_accepted(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=str(tmp_path), run_id="str-dir")
        assert store.run_dir.is_dir()


# ---------------------------------------------------------------------------
# write() / read()
# ---------------------------------------------------------------------------

class TestWriteRead:
    def test_write_and_read_single_segment(self, store: ArtifactStore) -> None:
        store.write("world", "hello.txt")
        assert store.read("hello.txt") == "world"

    def test_write_and_read_nested_segments(self, store: ArtifactStore) -> None:
        store.write("payload", "round_1", "inner_1", "proposal.txt")
        assert store.read("round_1", "inner_1", "proposal.txt") == "payload"

    def test_write_creates_parent_dirs(self, store: ArtifactStore, tmp_path: Path) -> None:
        store.write("data", "a", "b", "c", "deep.txt")
        assert (tmp_path / "test-run-001" / "a" / "b" / "c" / "deep.txt").exists()

    def test_write_returns_path(self, store: ArtifactStore) -> None:
        p = store.write("data", "file.txt")
        assert isinstance(p, Path)
        assert p.name == "file.txt"

    def test_write_overwrites_existing(self, store: ArtifactStore) -> None:
        store.write("first", "file.txt")
        store.write("second", "file.txt")
        assert store.read("file.txt") == "second"

    def test_read_missing_returns_empty_string(self, store: ArtifactStore) -> None:
        assert store.read("nonexistent.txt") == ""

    def test_read_missing_nested_returns_empty_string(self, store: ArtifactStore) -> None:
        assert store.read("no", "such", "path.txt") == ""

    def test_write_empty_string(self, store: ArtifactStore) -> None:
        store.write("", "empty.txt")
        assert store.read("empty.txt") == ""

    def test_write_unicode_content(self, store: ArtifactStore) -> None:
        content = "héllo wörld 🎉"
        store.write(content, "unicode.txt")
        assert store.read("unicode.txt") == content

    def test_write_multiline_content(self, store: ArtifactStore) -> None:
        lines = "line1\nline2\nline3"
        store.write(lines, "multi.txt")
        assert store.read("multi.txt") == lines


# ---------------------------------------------------------------------------
# exists()
# ---------------------------------------------------------------------------

class TestExists:
    def test_returns_false_for_missing(self, store: ArtifactStore) -> None:
        assert store.exists("nonexistent.txt") is False

    def test_returns_true_after_write(self, store: ArtifactStore) -> None:
        store.write("hi", "present.txt")
        assert store.exists("present.txt") is True

    def test_returns_false_for_wrong_name(self, store: ArtifactStore) -> None:
        store.write("data", "actual.txt")
        assert store.exists("other.txt") is False

    def test_exists_with_nested_segments(self, store: ArtifactStore) -> None:
        store.write("nested", "a", "b.txt")
        assert store.exists("a", "b.txt") is True
        assert store.exists("a", "missing.txt") is False


# ---------------------------------------------------------------------------
# Run-level markers: write_final_summary / is_complete
# ---------------------------------------------------------------------------

class TestRunMarkers:
    def test_is_complete_false_initially(self, store: ArtifactStore) -> None:
        assert store.is_complete is False

    def test_write_final_summary_marks_complete(self, store: ArtifactStore) -> None:
        store.write_final_summary("All done.")
        assert store.is_complete is True

    def test_final_summary_content_readable(self, store: ArtifactStore) -> None:
        store.write_final_summary("Summary text.")
        assert store.read("final_summary.md") == "Summary text."

    def test_write_final_summary_returns_path(self, store: ArtifactStore) -> None:
        p = store.write_final_summary("done")
        assert p.name == "final_summary.md"


# ---------------------------------------------------------------------------
# inner_dir() / phase_dir()
# ---------------------------------------------------------------------------

class TestDirHelpers:
    def test_inner_dir_zero_based(self, store: ArtifactStore) -> None:
        segs = store.inner_dir(outer=0, phase_label="1_dev", inner=0)
        assert segs == ("round_1", "phase_1_dev", "inner_1")

    def test_inner_dir_nonzero(self, store: ArtifactStore) -> None:
        segs = store.inner_dir(outer=2, phase_label="3_test", inner=4)
        assert segs == ("round_3", "phase_3_test", "inner_5")

    def test_phase_dir_zero_based(self, store: ArtifactStore) -> None:
        segs = store.phase_dir(outer=0, phase_label="1_requirements")
        assert segs == ("round_1", "phase_1_requirements")

    def test_phase_dir_nonzero(self, store: ArtifactStore) -> None:
        segs = store.phase_dir(outer=3, phase_label="2_implementation")
        assert segs == ("round_4", "phase_2_implementation")

    def test_inner_dir_can_write_file(self, store: ArtifactStore) -> None:
        segs = store.inner_dir(outer=0, phase_label="1_code", inner=0)
        store.write("proposal body", *segs, "proposal.txt")
        assert store.read(*segs, "proposal.txt") == "proposal body"


# ---------------------------------------------------------------------------
# find_resumable()
# ---------------------------------------------------------------------------

class TestFindResumable:
    def test_returns_none_for_missing_dir(self, tmp_path: Path) -> None:
        result = ArtifactStore.find_resumable(tmp_path / "missing")
        assert result is None

    def test_returns_none_for_empty_dir(self, tmp_path: Path) -> None:
        result = ArtifactStore.find_resumable(tmp_path)
        assert result is None

    def test_returns_none_when_run_complete(self, tmp_path: Path) -> None:
        store = ArtifactStore(base_dir=tmp_path, run_id="done-run")
        # Create a round_* dir so it looks like a real run
        (store.run_dir / "round_1").mkdir()
        store.write_final_summary("done")
        result = ArtifactStore.find_resumable(tmp_path)
        assert result is None

    def test_returns_store_for_incomplete_run(self, tmp_path: Path) -> None:
        # run_id must match the "run_*" glob pattern
        store = ArtifactStore(base_dir=tmp_path, run_id="run_20230601T120000")
        (store.run_dir / "round_1").mkdir()
        # No final_summary.md — run is incomplete
        found = ArtifactStore.find_resumable(tmp_path)
        assert found is not None
        assert found.run_dir == store.run_dir

    def test_returns_most_recent_incomplete(self, tmp_path: Path) -> None:
        # Create two incomplete runs; find_resumable should return the most recent
        old = ArtifactStore(base_dir=tmp_path, run_id="run_20230101T000000")
        new = ArtifactStore(base_dir=tmp_path, run_id="run_20240101T000000")
        (old.run_dir / "round_1").mkdir()
        (new.run_dir / "round_1").mkdir()
        found = ArtifactStore.find_resumable(tmp_path)
        assert found is not None
        assert found.run_dir == new.run_dir
