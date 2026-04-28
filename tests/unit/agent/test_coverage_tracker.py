"""Unit tests for CoverageTracker and collect_project_files (US-11)."""

import os
import pytest

from harness.agent.coverage_tracker import (
    CoverageTracker,
    CoverageReport,
    collect_project_files,
)


def test_update_accumulates_across_calls():
    tracker = CoverageTracker()
    tracker.update(["a.py", "b.py"], ["c.py"])
    tracker.update(["b.py", "d.py"], ["e.py"])

    # Read set should be {a.py, b.py, d.py}
    assert tracker._read_paths == {"a.py", "b.py", "d.py"}
    # Written set should be {c.py, e.py}
    assert tracker._written_paths == {"c.py", "e.py"}


def test_report_basic():
    tracker = CoverageTracker()
    tracker.update(["a.py", "b.py"], ["c.py"])

    project_files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    report = tracker.report(project_files)

    assert report.total_project_files == 5
    assert report.files_read == 2   # a.py, b.py
    assert report.files_written == 1  # c.py
    assert report.files_touched == 3  # a, b, c
    assert report.coverage_ratio == pytest.approx(0.6)
    assert set(report.untouched_files) == {"d.py", "e.py"}


def test_report_empty_tracker():
    tracker = CoverageTracker()
    project_files = ["a.py", "b.py"]
    report = tracker.report(project_files)

    assert report.total_project_files == 2
    assert report.files_touched == 0
    assert report.coverage_ratio == 0.0
    assert len(report.untouched_files) == 2


def test_report_empty_project():
    tracker = CoverageTracker()
    tracker.update(["a.py"], [])
    report = tracker.report([])

    assert report.total_project_files == 0
    assert report.coverage_ratio == 0.0


def test_report_only_counts_project_files():
    """Files read/written outside the project file list are not counted."""
    tracker = CoverageTracker()
    tracker.update(["/abs/path/x.py"], ["outside.py"])

    project_files = ["a.py", "b.py"]
    report = tracker.report(project_files)

    assert report.files_read == 0
    assert report.files_written == 0
    assert report.files_touched == 0


def test_report_untouched_capped():
    """Untouched files list is capped at 50."""
    tracker = CoverageTracker()
    project_files = [f"file_{i}.py" for i in range(100)]
    report = tracker.report(project_files)

    assert len(report.untouched_files) == 50


def test_format_report_includes_key_data():
    report = CoverageReport(
        total_project_files=100,
        files_read=30,
        files_written=10,
        files_touched=35,
        coverage_ratio=0.35,
        untouched_files=["untouched_a.py", "untouched_b.py"],
    )
    text = CoverageTracker.format_report(report)

    assert "100" in text
    assert "35" in text
    assert "35.0%" in text
    assert "untouched_a.py" in text
    assert "untouched_b.py" in text


def test_collect_project_files_on_harness(tmp_path):
    """Test collect_project_files against a synthetic workspace."""
    # Create some files
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "README.md").write_text("# Readme")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").write_text("pass")
    (sub / "style.css").write_text("body {}")  # not in PROJECT_EXTENSIONS

    # Create a skip dir
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "cached.py").write_text("cached")

    result = collect_project_files(str(tmp_path))

    # Should include .py, .json, .md but not .css or __pycache__ contents
    assert "main.py" in result
    assert "config.json" in result
    assert "README.md" in result
    assert os.path.join("src", "app.py") in result
    assert os.path.join("src", "style.css") not in result
    assert any("cached" in r for r in result) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
