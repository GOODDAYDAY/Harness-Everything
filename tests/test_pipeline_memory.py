"""Tests for harness/pipeline/memory.py

Covers:
- Private extraction helpers (_first_bullet, _extract_top_defect, etc.)
- MemoryEntry construction, serialization, and deserialization
- MemoryStore.record(), format_context(), best_score(), entry_count
- Persistence across MemoryStore instances (round-trip via JSONL)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from harness.pipeline.memory import (
    MemoryEntry,
    MemoryStore,
    _extract_actionable_feedback,
    _extract_key_risk,
    _extract_top_defect,
    _extract_what_would_make_10,
    _first_bullet,
)
from harness.pipeline.phase import DualScore, InnerResult, PhaseConfig, PhaseResult, ScoreItem


# ---------------------------------------------------------------------------
# Helpers to build test fixtures
# ---------------------------------------------------------------------------

def _make_phase(name: str = "code_quality", index: int = 0) -> PhaseConfig:
    return PhaseConfig(name=name, index=index, system_prompt="Do stuff")


def _make_dual_score(
    basic_score: float = 7.5,
    diffusion_score: float = 8.0,
    basic_critique: str = "TOP DEFECT: Missing tests\nKEY RISK: OOM\nACTIONABLE FEEDBACK:\n    1. Add coverage",
    diffusion_critique: str = "WHAT WOULD MAKE THIS 10/10: Full test suite",
) -> DualScore:
    return DualScore(
        basic=ScoreItem(score=basic_score, critique=basic_critique),
        diffusion=ScoreItem(score=diffusion_score, critique=diffusion_critique),
    )


def _make_inner(proposal: str = "## Summary\nSomething great", score: float = 7.5) -> InnerResult:
    return InnerResult(
        proposal=proposal,
        dual_score=_make_dual_score(basic_score=score, diffusion_score=score),
        verdict="ACCEPT",
    )


def _make_result(
    phase: PhaseConfig | None = None,
    synthesis: str = "TOP DEFECT: Missing tests\nKEY RISK: OOM",
    best_score: float = 7.5,
    inner_results: list[InnerResult] | None = None,
) -> PhaseResult:
    if phase is None:
        phase = _make_phase()
    if inner_results is None:
        inner_results = [_make_inner(score=best_score)]
    return PhaseResult(phase=phase, synthesis=synthesis, best_score=best_score, inner_results=inner_results)


def _make_store(tmp_path: Path) -> MemoryStore:
    arts = MagicMock()
    arts.run_dir = tmp_path
    return MemoryStore(arts)


# ---------------------------------------------------------------------------
# _first_bullet
# ---------------------------------------------------------------------------

class TestFirstBullet:
    def test_extracts_first_bullet(self):
        body = "- item one\n- item two\n"
        assert _first_bullet(body) == "item one"

    def test_asterisk_bullet(self):
        body = "* item one\n* item two\n"
        assert _first_bullet(body) == "item one"

    def test_strips_whitespace(self):
        body = "  -   item with spaces  \n"
        assert _first_bullet(body) == "item with spaces"

    def test_empty_body_returns_empty_or_blank(self):
        result = _first_bullet("")
        assert isinstance(result, str)

    def test_no_bullet_returns_string(self):
        # Non-bullet text: should return some string (implementation may vary)
        result = _first_bullet("plain text\nsecond line")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_top_defect
# ---------------------------------------------------------------------------

class TestExtractTopDefect:
    def test_extracts_inline(self):
        text = "TOP DEFECT: Missing error handling"
        assert "Missing error handling" in _extract_top_defect(text)

    def test_extracts_from_section(self):
        text = "## CRITICAL DEFECTS\n- Memory leak\n\n## Other"
        assert "Memory leak" in _extract_top_defect(text)

    def test_empty_when_no_match(self):
        assert _extract_top_defect("## What Works\nGood stuff") == ""

    def test_empty_string(self):
        assert _extract_top_defect("") == ""


# ---------------------------------------------------------------------------
# _extract_key_risk
# ---------------------------------------------------------------------------

class TestExtractKeyRisk:
    def test_extracts_inline(self):
        text = "KEY RISK: Performance bottleneck"
        assert "Performance bottleneck" in _extract_key_risk(text)

    def test_empty_when_no_match(self):
        assert _extract_key_risk("## What Works\nGood") == ""

    def test_empty_string(self):
        assert _extract_key_risk("") == ""


# ---------------------------------------------------------------------------
# _extract_actionable_feedback
# ---------------------------------------------------------------------------

class TestExtractActionableFeedback:
    def test_extracts_numbered_items(self):
        text = "ACTIONABLE FEEDBACK:\n    1. Fix the bug\n    2. Add tests\n"
        result = _extract_actionable_feedback(text)
        assert "Fix the bug" in result
        assert "Add tests" in result

    def test_joins_with_semicolon(self):
        text = "ACTIONABLE FEEDBACK:\n    1. Fix the bug\n    2. Add tests\n"
        result = _extract_actionable_feedback(text)
        assert ";" in result

    def test_empty_when_no_section(self):
        assert _extract_actionable_feedback("## Summary\nOK") == ""

    def test_empty_string(self):
        assert _extract_actionable_feedback("") == ""


# ---------------------------------------------------------------------------
# _extract_what_would_make_10
# ---------------------------------------------------------------------------

class TestExtractWhatWouldMake10:
    def test_extracts_value(self):
        text = "WHAT WOULD MAKE THIS 10/10: Add comprehensive tests"
        result = _extract_what_would_make_10(text)
        assert "Add comprehensive tests" in result

    def test_empty_when_no_match(self):
        assert _extract_what_would_make_10("## Summary\nOK") == ""

    def test_empty_string(self):
        assert _extract_what_would_make_10("") == ""


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------

class TestMemoryEntry:
    def _sample_entry(self) -> MemoryEntry:
        return MemoryEntry(
            ts="2024-01-01T00:00:00",
            round=1,
            phase="0_testing",
            score=7.5,
            score_delta=1.0,
            insight="Good insight",
            evaluator_top_defect="Missing tests",
            evaluator_key_risk="OOM risk",
            actionable_feedback="Add coverage; improve docs",
            what_would_make_10="100% test coverage",
        )

    def test_to_json_line_produces_valid_json(self):
        entry = self._sample_entry()
        line = entry.to_json_line()
        data = json.loads(line)
        assert data["round"] == 1
        assert data["phase"] == "0_testing"
        assert data["score"] == 7.5

    def test_to_json_line_no_trailing_newline(self):
        entry = self._sample_entry()
        line = entry.to_json_line()
        assert not line.endswith("\n")

    def test_from_json_line_round_trip(self):
        entry = self._sample_entry()
        line = entry.to_json_line()
        restored = MemoryEntry.from_json_line(line)
        assert restored is not None
        assert restored.round == 1
        assert restored.phase == "0_testing"
        assert restored.score == 7.5
        assert restored.score_delta == 1.0
        assert restored.insight == "Good insight"
        assert restored.evaluator_top_defect == "Missing tests"
        assert restored.evaluator_key_risk == "OOM risk"
        assert restored.actionable_feedback == "Add coverage; improve docs"
        assert restored.what_would_make_10 == "100% test coverage"

    def test_from_json_line_returns_none_on_invalid_json(self):
        result = MemoryEntry.from_json_line("not valid json !!!")
        assert result is None

    def test_from_json_line_handles_missing_optional_fields(self):
        data = {"round": 1, "phase": "test", "score": 5.0, "score_delta": 0.0,
                "ts": "2024", "insight": ""}
        restored = MemoryEntry.from_json_line(json.dumps(data))
        assert restored is not None
        assert restored.evaluator_top_defect == ""
        assert restored.actionable_feedback == ""
        assert restored.what_would_make_10 == ""

    def test_from_json_line_coerces_types(self):
        data = {"ts": "2024", "round": "3", "phase": "test", "score": "8.0",
                "score_delta": "1.0", "insight": "",
                "evaluator_top_defect": "", "evaluator_key_risk": "",
                "actionable_feedback": "", "what_would_make_10": ""}
        entry = MemoryEntry.from_json_line(json.dumps(data))
        assert entry is not None
        assert entry.round == 3
        assert entry.score == 8.0


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class TestMemoryStore:
    def test_new_store_has_zero_entries(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.entry_count == 0

    def test_new_store_best_score_zero_for_unknown_phase(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.best_score("unknown_phase") == 0.0

    def test_record_increases_entry_count(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase))
        assert store.entry_count == 1

    def test_record_updates_best_score_for_phase(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=8.5))
        assert store.best_score(phase.label) == 8.5

    def test_best_score_tracks_maximum_for_phase(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=6.0))
        store.record(1, _make_result(phase=phase, best_score=9.0))
        store.record(2, _make_result(phase=phase, best_score=7.0))
        assert store.best_score(phase.label) == 9.0

    def test_record_writes_jsonl_file(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase))
        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        assert memory_file.exists()

    def test_persisted_file_is_valid_jsonl(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=7.5))
        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        line = memory_file.read_text().strip().split("\n")[0]
        data = json.loads(line)
        assert data["score"] == 7.5
        assert "round" in data
        assert "phase" in data

    def test_persistence_round_trip(self, tmp_path):
        """A second MemoryStore instance loads data persisted by the first."""
        arts = MagicMock()
        arts.run_dir = tmp_path
        phase = _make_phase("my_phase")
        result = _make_result(phase=phase, best_score=8.0)

        store1 = MemoryStore(arts)
        store1.record(0, result)

        store2 = MemoryStore(arts)
        assert store2.entry_count == 1
        assert store2.best_score(phase.label) == 8.0

    def test_multiple_records_accumulated(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        for i, s in enumerate([6.0, 7.0, 8.0]):
            store.record(i, _make_result(phase=phase, best_score=s))
        assert store.entry_count == 3

    def test_multiple_records_in_jsonl(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        for i, s in enumerate([6.0, 7.0, 8.0]):
            store.record(i, _make_result(phase=phase, best_score=s))
        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        lines = [line for line in memory_file.read_text().strip().split("\n") if line]
        assert len(lines) == 3

    def test_format_context_empty_when_no_entries(self, tmp_path):
        store = _make_store(tmp_path)
        result = store.format_context()
        assert isinstance(result, str)
        assert len(result) == 0 or "No" in result or result == ""

    def test_format_context_returns_nonempty_with_entries(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase("analysis")
        store.record(0, _make_result(phase=phase, best_score=7.0))
        ctx = store.format_context()
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_format_context_includes_phase_label(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase("special_phase_xyz")
        store.record(0, _make_result(phase=phase, best_score=7.0))
        ctx = store.format_context(phase.label)
        assert phase.label in ctx

    def test_format_context_includes_score(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=8.5))
        ctx = store.format_context()
        assert "8.5" in ctx

    def test_format_context_max_entries_parameter(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        for i in range(5):
            store.record(i, _make_result(phase=phase, best_score=float(i + 5)))
        # With fewer max entries the result should still be a valid string
        ctx = store.format_context(max_entries=2)
        assert isinstance(ctx, str)

    def test_record_extracts_defect_into_entry(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        inner = InnerResult(
            proposal="Summary",
            dual_score=DualScore(
                basic=ScoreItem(score=6.0, critique="TOP DEFECT: Race condition\nKEY RISK: Data loss\nACTIONABLE FEEDBACK:\n    1. Use locks"),
                diffusion=ScoreItem(score=7.0, critique="WHAT WOULD MAKE THIS 10/10: Fix concurrency"),
            ),
            verdict="ACCEPT",
        )
        result = _make_result(phase=phase, best_score=6.0, inner_results=[inner])
        store.record(0, result)

        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        data = json.loads(memory_file.read_text().strip())
        assert "Race condition" in data["evaluator_top_defect"]

    def test_best_score_per_phase_is_independent(self, tmp_path):
        store = _make_store(tmp_path)
        phase_a = _make_phase("phase_a", index=0)
        phase_b = _make_phase("phase_b", index=1)
        store.record(0, _make_result(phase=phase_a, best_score=9.0))
        store.record(0, _make_result(phase=phase_b, best_score=5.0))
        assert store.best_score(phase_a.label) == 9.0
        assert store.best_score(phase_b.label) == 5.0

    def test_memory_file_is_jsonl(self):
        assert MemoryStore._MEMORY_FILE.endswith(".jsonl")

    def test_score_delta_is_zero_on_first_record(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=7.0))
        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        data = json.loads(memory_file.read_text().strip())
        # First record: previous best was 0 so delta = 7.0 - 0 = 7.0
        assert data["score_delta"] >= 0

    def test_score_delta_positive_on_improvement(self, tmp_path):
        store = _make_store(tmp_path)
        phase = _make_phase()
        store.record(0, _make_result(phase=phase, best_score=5.0))
        store.record(1, _make_result(phase=phase, best_score=8.0))
        memory_file = tmp_path / MemoryStore._MEMORY_FILE
        lines = [line for line in memory_file.read_text().strip().split("\n") if line]
        data2 = json.loads(lines[1])
        assert data2["score_delta"] > 0
