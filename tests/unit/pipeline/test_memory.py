"""Tests for harness/pipeline/memory.py — MemoryStore and helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.pipeline.memory import (
    MemoryEntry,
    MemoryStore,
    _extract_actionable_feedback,
    _extract_key_risk,
    _extract_top_defect,
    _extract_what_would_make_10,
    _first_bullet,
)


# ---------------------------------------------------------------------------
# Helper: build a real ArtifactStore pointed at a tmp_path
# ---------------------------------------------------------------------------


def make_store(tmp_path: Path) -> MemoryStore:
    """Create a MemoryStore backed by a real ArtifactStore in tmp_path."""
    from harness.core.artifacts import ArtifactStore

    artifacts = ArtifactStore(base_dir=tmp_path, run_id="test_run")
    return MemoryStore(artifacts)


# ---------------------------------------------------------------------------
# Helper: build minimal PhaseResult objects without importing half the world
# ---------------------------------------------------------------------------


def make_phase_result(
    phase_name: str = "implementation",
    index: int = 0,
    synthesis: str = "Did something useful.",
    best_score: float = 7.5,
    basic_critique: str = "",
    diffusion_critique: str = "",
    inner_count: int = 1,
) -> object:
    """Build a minimal PhaseResult-like object usable by MemoryStore.record()."""
    from harness.pipeline.phase import (
        DualScore,
        InnerResult,
        PhaseConfig,
        PhaseResult,
        ScoreItem,
    )

    config = PhaseConfig(name=phase_name, index=index, system_prompt="", falsifiable_criterion="")

    inner_results = []
    for i in range(inner_count):
        score = best_score - (inner_count - 1 - i) * 0.5  # last is best
        dual = DualScore(
            basic=ScoreItem(score=score, critique=basic_critique),
            diffusion=ScoreItem(score=score, critique=diffusion_critique),
        )
        inner_results.append(InnerResult(proposal=f"proposal {i}", dual_score=dual))

    return PhaseResult(
        phase=config,
        synthesis=synthesis,
        best_score=best_score,
        inner_results=inner_results,
    )


# ===========================================================================
# MemoryEntry — serialization round-trip
# ===========================================================================


class TestMemoryEntryRoundTrip:
    def test_to_and_from_json_line_preserves_all_fields(self):
        entry = MemoryEntry(
            ts="2026-04-14T12:03:47",
            round=2,
            phase="1_requirements_analysis",
            score=14.5,
            score_delta=2.5,
            insight="The plan was solid.",
            evaluator_top_defect="module.py::func — missing guard",
            evaluator_key_risk="caller.py::call — may break on None",
            actionable_feedback="fix guard; add test",
            what_would_make_10="add strict type check in module.py::func",
        )
        line = entry.to_json_line()
        restored = MemoryEntry.from_json_line(line)

        assert restored is not None
        assert restored.ts == entry.ts
        assert restored.round == entry.round
        assert restored.phase == entry.phase
        assert restored.score == entry.score
        assert restored.score_delta == entry.score_delta
        assert restored.insight == entry.insight
        assert restored.evaluator_top_defect == entry.evaluator_top_defect
        assert restored.evaluator_key_risk == entry.evaluator_key_risk
        assert restored.actionable_feedback == entry.actionable_feedback
        assert restored.what_would_make_10 == entry.what_would_make_10

    def test_to_json_line_is_single_line(self):
        entry = MemoryEntry(
            ts="2026-04-14T00:00:00",
            round=1,
            phase="dev",
            score=8.0,
            score_delta=0.0,
            insight="line1\nline2",  # newlines get replaced in record() but test the serializer directly
            evaluator_top_defect="",
            evaluator_key_risk="",
            actionable_feedback="",
            what_would_make_10="",
        )
        line = entry.to_json_line()
        # Must be exactly one JSON line (no newline inside the serialized text)
        assert "\n" not in line

    def test_from_json_line_returns_none_on_invalid_json(self):
        result = MemoryEntry.from_json_line("not valid json{{")
        assert result is None

    def test_from_json_line_returns_none_on_empty_string(self):
        result = MemoryEntry.from_json_line("")
        assert result is None

    def test_from_json_line_fills_defaults_for_missing_keys(self):
        """from_json_line uses .get() with defaults — partial JSON should not raise."""
        partial = json.dumps({"round": 3, "phase": "dev", "score": 6.0})
        entry = MemoryEntry.from_json_line(partial)
        assert entry is not None
        assert entry.round == 3
        assert entry.score == 6.0
        assert entry.insight == ""
        assert entry.evaluator_top_defect == ""
        assert entry.evaluator_key_risk == ""
        assert entry.ts == ""
        assert entry.score_delta == 0.0

    def test_from_json_line_coerces_score_to_float(self):
        """Integer scores in JSON should be accepted and become floats."""
        line = json.dumps(
            {
                "ts": "",
                "round": 1,
                "phase": "x",
                "score": 8,
                "score_delta": 0,
                "insight": "",
                "evaluator_top_defect": "",
                "evaluator_key_risk": "",
            }
        )
        entry = MemoryEntry.from_json_line(line)
        assert entry is not None
        assert isinstance(entry.score, float)
        assert entry.score == 8.0


# ===========================================================================
# _first_bullet helper
# ===========================================================================


class TestFirstBullet:
    def test_extracts_numbered_bullet(self):
        body = "1. Missing error handler in process()"
        assert _first_bullet(body) == "Missing error handler in process()"

    def test_extracts_dash_bullet(self):
        body = "- The retry loop never terminates"
        assert _first_bullet(body) == "The retry loop never terminates"

    def test_strips_bold_markers(self):
        body = "1. **Correctness**: The function returns None"
        result = _first_bullet(body)
        assert "**" not in result
        assert "Correctness" in result

    def test_skips_blank_lines(self):
        body = "\n\n- Real content here"
        assert _first_bullet(body) == "Real content here"

    def test_empty_body_returns_empty_string(self):
        assert _first_bullet("") == ""
        assert _first_bullet("   \n  ") == ""


# ===========================================================================
# _extract_top_defect
# ===========================================================================


class TestExtractTopDefect:
    def test_canonical_form(self):
        text = "Some preamble\nTOP DEFECT: module.py::parse — incorrect regex\nMore text"
        assert _extract_top_defect(text) == "module.py::parse — incorrect regex"

    def test_canonical_form_case_insensitive(self):
        text = "top defect: another.py::fn — breaks on empty input"
        assert _extract_top_defect(text) == "another.py::fn — breaks on empty input"

    def test_canonical_form_truncates_at_200_chars(self):
        long_text = "TOP DEFECT: " + "x" * 300
        result = _extract_top_defect(long_text)
        assert len(result) <= 200

    def test_markdown_section_fallback(self):
        text = (
            "## CRITICAL DEFECTS\n"
            "1. The cache is never invalidated\n"
            "2. Another minor issue\n"
            "## NEXT SECTION\n"
            "Irrelevant\n"
        )
        result = _extract_top_defect(text)
        assert "cache is never invalidated" in result

    def test_markdown_section_fallback_strips_prefix(self):
        text = "## Critical Defect Found\n- **Cache**: never invalidated\n"
        result = _extract_top_defect(text)
        assert "**" not in result
        assert "Cache" in result or "never invalidated" in result

    def test_returns_empty_string_when_nothing_found(self):
        text = "Everything is fine.  No defects here."
        assert _extract_top_defect(text) == ""

    def test_canonical_preferred_over_section(self):
        """When both forms exist, the single-line form is returned (first match wins)."""
        text = (
            "TOP DEFECT: canonical.py::func — real issue\n"
            "## CRITICAL DEFECTS\n"
            "1. Section content\n"
        )
        result = _extract_top_defect(text)
        assert result == "canonical.py::func — real issue"


# ===========================================================================
# _extract_key_risk
# ===========================================================================


class TestExtractKeyRisk:
    def test_canonical_form(self):
        text = "KEY RISK: scheduler.py::run — tasks pile up under high load\n"
        assert _extract_key_risk(text) == "scheduler.py::run — tasks pile up under high load"

    def test_canonical_form_case_insensitive(self):
        text = "key risk: foo.py::bar — might deadlock"
        assert _extract_key_risk(text) == "foo.py::bar — might deadlock"

    def test_canonical_form_truncates_at_200_chars(self):
        long_text = "KEY RISK: " + "y" * 300
        result = _extract_key_risk(long_text)
        assert len(result) <= 200

    def test_markdown_key_risks_section_fallback(self):
        text = (
            "## KEY RISKS\n"
            "1. Memory leak in connection pool\n"
            "2. Another risk\n"
            "## OTHER SECTION\n"
        )
        result = _extract_key_risk(text)
        assert "Memory leak" in result or "memory leak" in result.lower()

    def test_markdown_second_order_effects_fallback(self):
        text = (
            "## SECOND-ORDER EFFECTS\n"
            "- Downstream consumers will fail silently\n"
        )
        result = _extract_key_risk(text)
        assert "Downstream consumers" in result or "downstream" in result.lower()

    def test_returns_empty_string_when_nothing_found(self):
        text = "All risks have been mitigated."
        assert _extract_key_risk(text) == ""

    def test_canonical_preferred_over_section(self):
        text = (
            "KEY RISK: canonical.py::func — the canonical risk\n"
            "## KEY RISKS\n"
            "1. section content\n"
        )
        result = _extract_key_risk(text)
        assert result == "canonical.py::func — the canonical risk"


# ===========================================================================
# MemoryStore.record()
# ===========================================================================


class TestMemoryStoreRecord:
    def test_record_creates_entry(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(phase_name="implementation", index=0, best_score=7.0)
        store.record(outer=0, phase_result=pr)
        assert store.entry_count == 1

    def test_record_first_round_delta_is_score(self, tmp_path):
        """First record for a phase: delta == score (prev_best == 0)."""
        store = make_store(tmp_path)
        pr = make_phase_result(best_score=7.0)
        store.record(outer=0, phase_result=pr)
        entry = store._entries[0]
        assert entry.score_delta == pytest.approx(7.0, abs=0.01)

    def test_record_second_round_delta_is_improvement(self, tmp_path):
        store = make_store(tmp_path)
        pr1 = make_phase_result(phase_name="impl", index=0, best_score=6.0)
        pr2 = make_phase_result(phase_name="impl", index=0, best_score=8.5)
        store.record(outer=0, phase_result=pr1)
        store.record(outer=1, phase_result=pr2)
        assert store.entry_count == 2
        # Second entry delta: 8.5 - 6.0 = 2.5
        assert store._entries[1].score_delta == pytest.approx(2.5, abs=0.01)

    def test_record_best_score_updated(self, tmp_path):
        store = make_store(tmp_path)
        pr1 = make_phase_result(phase_name="impl", index=0, best_score=6.0)
        pr2 = make_phase_result(phase_name="impl", index=0, best_score=8.5)
        store.record(outer=0, phase_result=pr1)
        store.record(outer=1, phase_result=pr2)
        assert store.best_score("1_impl") == pytest.approx(8.5, abs=0.01)

    def test_record_best_score_not_regressed_by_lower_score(self, tmp_path):
        store = make_store(tmp_path)
        pr1 = make_phase_result(phase_name="impl", index=0, best_score=8.5)
        pr2 = make_phase_result(phase_name="impl", index=0, best_score=5.0)
        store.record(outer=0, phase_result=pr1)
        store.record(outer=1, phase_result=pr2)
        # Best score should remain 8.5, not drop to 5.0
        assert store.best_score("1_impl") == pytest.approx(8.5, abs=0.01)

    def test_record_extracts_top_defect_from_best_inner(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(
            best_score=7.0,
            basic_critique="TOP DEFECT: utils.py::helper — off-by-one error\n",
        )
        store.record(outer=0, phase_result=pr)
        assert store._entries[0].evaluator_top_defect == "utils.py::helper — off-by-one error"

    def test_record_extracts_key_risk_from_best_inner(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(
            best_score=7.0,
            diffusion_critique="KEY RISK: api.py::handler — unhandled 500 errors\n",
        )
        store.record(outer=0, phase_result=pr)
        assert store._entries[0].evaluator_key_risk == "api.py::handler — unhandled 500 errors"

    def test_record_outer_index_becomes_1based_round(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result()
        store.record(outer=2, phase_result=pr)  # 0-based index 2 → round 3
        assert store._entries[0].round == 3

    def test_record_insight_truncated_for_non_design_phases(self, tmp_path):
        """Non-design/orchestrate phases are capped at 800 chars."""
        store = make_store(tmp_path)
        long_synthesis = "A" * 2000
        pr = make_phase_result(phase_name="implementation", synthesis=long_synthesis)
        store.record(outer=0, phase_result=pr)
        assert len(store._entries[0].insight) <= 800

    def test_record_insight_longer_for_design_phases(self, tmp_path):
        """Design/orchestrate phases allow up to 2000 chars."""
        store = make_store(tmp_path)
        long_synthesis = "B" * 2000
        pr = make_phase_result(phase_name="design", synthesis=long_synthesis)
        store.record(outer=0, phase_result=pr)
        assert len(store._entries[0].insight) <= 2000
        # And it should be longer than the non-design limit
        assert len(store._entries[0].insight) > 800

    def test_record_writes_to_disk(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result()
        store.record(outer=0, phase_result=pr)
        memory_file = tmp_path / "test_run" / "memory.jsonl"
        assert memory_file.exists()
        lines = [ln for ln in memory_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

    def test_record_multiple_entries_written_to_disk(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(3):
            pr = make_phase_result(phase_name=f"phase_{i}", index=i)
            store.record(outer=i, phase_result=pr)
        memory_file = tmp_path / "test_run" / "memory.jsonl"
        lines = [ln for ln in memory_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3


# ===========================================================================
# MemoryStore.format_context()
# ===========================================================================


class TestMemoryStoreFormatContext:
    def test_returns_empty_string_when_no_entries(self, tmp_path):
        store = make_store(tmp_path)
        assert store.format_context() == ""

    def test_returns_non_empty_string_after_record(self, tmp_path):
        store = make_store(tmp_path)
        store.record(outer=0, phase_result=make_phase_result(synthesis="Insight here"))
        ctx = store.format_context()
        assert ctx != ""
        assert "Prior Round Learnings" in ctx

    def test_includes_phase_specific_entries_first(self, tmp_path):
        store = make_store(tmp_path)
        store.record(outer=0, phase_result=make_phase_result(phase_name="impl", index=0))
        store.record(outer=0, phase_result=make_phase_result(phase_name="review", index=1))
        # Ask for 'impl' phase — it should appear before 'review'
        ctx = store.format_context(phase_label="1_impl")
        impl_pos = ctx.find("1_impl")
        review_pos = ctx.find("2_review")
        assert impl_pos != -1, "phase-specific entry must appear in context"
        assert review_pos != -1, "cross-phase entry must appear in context"
        assert impl_pos < review_pos, "phase-specific entry must come first"

    def test_max_entries_cap_respected(self, tmp_path):
        store = make_store(tmp_path)
        for i in range(10):
            pr = make_phase_result(phase_name=f"phase_{i}", index=i)
            store.record(outer=i, phase_result=pr)
        ctx = store.format_context(max_entries=3)
        # Count section headers — each entry produces one "### Round N"
        header_count = ctx.count("### Round")
        assert header_count <= 3

    def test_returns_empty_string_when_max_entries_zero(self, tmp_path):
        """max_entries=0 should produce an empty context (no entries selected)."""
        store = make_store(tmp_path)
        store.record(outer=0, phase_result=make_phase_result())
        ctx = store.format_context(max_entries=0)
        assert ctx == ""

    def test_insight_shown_in_output(self, tmp_path):
        store = make_store(tmp_path)
        store.record(
            outer=0,
            phase_result=make_phase_result(synthesis="The plan was solid and well-executed."),
        )
        ctx = store.format_context()
        assert "The plan was solid and well-executed." in ctx

    def test_insight_trimmed_at_600_chars_in_output(self, tmp_path):
        """format_context() clips insights to 600 chars (display budget)."""
        store = make_store(tmp_path)
        # 800-char synthesis to exceed the display cap
        pr = make_phase_result(phase_name="implementation", synthesis="Z" * 800)
        store.record(outer=0, phase_result=pr)
        ctx = store.format_context()
        assert "[trimmed for prompt" in ctx

    def test_defect_and_risk_rendered_when_present(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(
            basic_critique="TOP DEFECT: file.py::fn — the concrete defect\n",
            diffusion_critique="KEY RISK: other.py::fn — the concrete risk\n",
        )
        store.record(outer=0, phase_result=pr)
        ctx = store.format_context()
        assert "Top defect to fix" in ctx
        assert "Key risk to address" in ctx
        assert "the concrete defect" in ctx
        assert "the concrete risk" in ctx

    def test_none_defect_not_rendered(self, tmp_path):
        """Entries with 'none' defects/risks must not emit the label at all."""
        from harness.pipeline.memory import MemoryEntry

        store = make_store(tmp_path)
        store._entries.append(
            MemoryEntry(
                ts="2026-01-01T00:00:00",
                round=1,
                phase="dev",
                score=8.0,
                score_delta=0.0,
                insight="good work",
                evaluator_top_defect="none",
                evaluator_key_risk="None",
                actionable_feedback="",
                what_would_make_10="",
            )
        )
        ctx = store.format_context()
        assert "Top defect to fix" not in ctx
        assert "Key risk to address" not in ctx

    def test_no_phase_label_shows_at_most_two_other_entries(self, tmp_path):
        """When phase_label=None all entries are in the 'other' bucket.
        The other_cap is min(2, max_entries), so at most 2 entries are shown
        regardless of max_entries — this is intentional design.
        """
        store = make_store(tmp_path)
        for i in range(4):
            pr = make_phase_result(phase_name=f"phase_{i}", index=i)
            store.record(outer=i, phase_result=pr)
        # With no phase label, all entries land in 'other' bucket
        ctx = store.format_context(phase_label=None, max_entries=10)
        # At most 2 entries shown (other_cap = min(2, max_entries - 0) = 2)
        header_count = ctx.count("### Round")
        assert header_count <= 2
        # The most recent entries should appear
        assert "4_phase_3" in ctx or "3_phase_2" in ctx


# ===========================================================================
# MemoryStore._load() — resume + malformed line tolerance
# ===========================================================================


class TestMemoryStoreLoad:
    def test_empty_file_loads_zero_entries(self, tmp_path):
        """An empty memory.jsonl produces zero entries after _load()."""
        memory_dir = tmp_path / "test_run"
        memory_dir.mkdir()
        (memory_dir / "memory.jsonl").write_text("", encoding="utf-8")

        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore.__new__(ArtifactStore)
        artifacts.run_dir = memory_dir
        store = MemoryStore(artifacts)
        assert store.entry_count == 0

    def test_malformed_line_is_skipped_not_raised(self, tmp_path):
        """Corrupt lines must not prevent loading valid surrounding entries."""
        memory_dir = tmp_path / "test_run"
        memory_dir.mkdir()

        good_entry = MemoryEntry(
            ts="2026-01-01T00:00:00",
            round=1,
            phase="dev",
            score=7.0,
            score_delta=1.0,
            insight="good",
            evaluator_top_defect="",
            evaluator_key_risk="",
            actionable_feedback="",
            what_would_make_10="",
        )
        lines = [
            good_entry.to_json_line(),
            "{invalid json{{{",          # malformed
            good_entry.to_json_line(),   # another valid entry
        ]
        (memory_dir / "memory.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore.__new__(ArtifactStore)
        artifacts.run_dir = memory_dir
        store = MemoryStore(artifacts)
        assert store.entry_count == 2  # 2 good, 1 skipped

    def test_blank_lines_are_skipped(self, tmp_path):
        memory_dir = tmp_path / "test_run"
        memory_dir.mkdir()

        entry = MemoryEntry(
            ts="2026-01-01T00:00:00",
            round=1,
            phase="dev",
            score=7.0,
            score_delta=0.0,
            insight="",
            evaluator_top_defect="",
            evaluator_key_risk="",
            actionable_feedback="",
            what_would_make_10="",
        )
        content = "\n" + entry.to_json_line() + "\n\n"
        (memory_dir / "memory.jsonl").write_text(content, encoding="utf-8")

        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore.__new__(ArtifactStore)
        artifacts.run_dir = memory_dir
        store = MemoryStore(artifacts)
        assert store.entry_count == 1

    def test_load_rebuilds_best_score_index(self, tmp_path):
        """After loading prior entries, best_score() should reflect loaded data."""
        memory_dir = tmp_path / "test_run"
        memory_dir.mkdir()

        entries = [
            MemoryEntry(
                ts="2026-01-01T00:00:00",
                round=1,
                phase="dev",
                score=6.0,
                score_delta=6.0,
                insight="",
                evaluator_top_defect="",
                evaluator_key_risk="",
                actionable_feedback="",
                what_would_make_10="",
            ),
            MemoryEntry(
                ts="2026-01-01T00:01:00",
                round=2,
                phase="dev",
                score=9.0,
                score_delta=3.0,
                insight="",
                evaluator_top_defect="",
                evaluator_key_risk="",
                actionable_feedback="",
                what_would_make_10="",
            ),
        ]
        lines = "\n".join(e.to_json_line() for e in entries) + "\n"
        (memory_dir / "memory.jsonl").write_text(lines, encoding="utf-8")

        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore.__new__(ArtifactStore)
        artifacts.run_dir = memory_dir
        store = MemoryStore(artifacts)
        assert store.best_score("dev") == pytest.approx(9.0, abs=0.01)


# ===========================================================================
# Full disk persistence cycle: write then reload
# ===========================================================================


class TestMemoryStoreDiskPersistence:
    def test_reload_produces_same_entry_count(self, tmp_path):
        """Entries written by record() survive a MemoryStore re-instantiation."""
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="persist_run")
        store1 = MemoryStore(artifacts)
        for i in range(3):
            pr = make_phase_result(phase_name=f"phase_{i}", index=i, best_score=5.0 + i)
            store1.record(outer=i, phase_result=pr)

        # Re-create store from same run dir
        store2 = MemoryStore(artifacts)
        assert store2.entry_count == 3

    def test_reload_restores_best_score(self, tmp_path):
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="persist_run_2")
        store1 = MemoryStore(artifacts)
        pr = make_phase_result(phase_name="impl", index=0, best_score=8.5)
        store1.record(outer=0, phase_result=pr)

        store2 = MemoryStore(artifacts)
        assert store2.best_score("1_impl") == pytest.approx(8.5, abs=0.01)

    def test_reload_restores_insights_and_feedback(self, tmp_path):
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="persist_run_3")
        store1 = MemoryStore(artifacts)
        pr = make_phase_result(
            synthesis="The approach was correct.",
            basic_critique="TOP DEFECT: x.py::y — the defect\n",
            diffusion_critique="KEY RISK: z.py::w — the risk\n",
        )
        store1.record(outer=0, phase_result=pr)

        store2 = MemoryStore(artifacts)
        entry = store2._entries[0]
        assert "The approach was correct." in entry.insight
        assert entry.evaluator_top_defect == "x.py::y — the defect"
        assert entry.evaluator_key_risk == "z.py::w — the risk"

    def test_append_after_reload(self, tmp_path):
        """After reload, new records are correctly appended to the file."""
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="append_run")
        store1 = MemoryStore(artifacts)
        store1.record(outer=0, phase_result=make_phase_result())

        store2 = MemoryStore(artifacts)
        store2.record(outer=1, phase_result=make_phase_result())

        store3 = MemoryStore(artifacts)
        assert store3.entry_count == 2


# ===========================================================================
# MemoryStore property/helper accessors
# ===========================================================================


class TestMemoryStoreHelpers:
    def test_entry_count_starts_at_zero(self, tmp_path):
        store = make_store(tmp_path)
        assert store.entry_count == 0

    def test_best_score_returns_zero_for_unknown_phase(self, tmp_path):
        store = make_store(tmp_path)
        assert store.best_score("no_such_phase") == 0.0

    def test_multiple_phases_tracked_independently(self, tmp_path):
        store = make_store(tmp_path)
        pr_a = make_phase_result(phase_name="alpha", index=0, best_score=6.0)
        pr_b = make_phase_result(phase_name="beta", index=1, best_score=9.0)
        store.record(outer=0, phase_result=pr_a)
        store.record(outer=0, phase_result=pr_b)
        assert store.best_score("1_alpha") == pytest.approx(6.0, abs=0.01)
        assert store.best_score("2_beta") == pytest.approx(9.0, abs=0.01)


# ---------------------------------------------------------------------------
# Tests for _extract_actionable_feedback
# ---------------------------------------------------------------------------

_BASIC_CRITIQUE_WITH_FEEDBACK = """\
CORRECTNESS SCORE: 7/10

TOP DEFECT:
  auth.py::login — password hash not salted

ACTIONABLE FEEDBACK:
  1. auth.py::login — add bcrypt salt before hashing the password
  2. auth.py::register — validate email format before storing
  3. session.py::create — set HTTPOnly flag on session cookie

WHAT WOULD MAKE THIS 10/10: extract auth logic into a dedicated AuthService class
"""

_BASIC_CRITIQUE_SINGLE_ITEM = """\
ACTIONABLE FEEDBACK:
  1. utils.py::parse_date — handle empty string input

WHAT WOULD MAKE THIS 10/10: add comprehensive edge-case tests for date parsing
"""

_BASIC_CRITIQUE_NO_FEEDBACK = """\
CORRECTNESS SCORE: 9/10

TOP DEFECT: None

KEY RISK: None
"""

_BASIC_CRITIQUE_ALREADY_PERFECT = """\
ACTIONABLE FEEDBACK:
  1. Nothing critical remains

WHAT WOULD MAKE THIS 10/10: it is already perfect, no changes needed
"""

_DIFFUSION_CRITIQUE_WITH_MITIGATIONS = """\
DIFFUSION SCORE: 6/10

ACTIONABLE MITIGATIONS:
  1. api.py::handler — add rate limiting to prevent abuse
  2. db.py::query — parameterise all SQL to prevent injection

WHAT WOULD MAKE THIS 10/10: introduce a dedicated input-validation layer
"""


class TestExtractActionableFeedback:
    def test_extracts_top_two_items_from_feedback(self):
        result = _extract_actionable_feedback(_BASIC_CRITIQUE_WITH_FEEDBACK)
        # Should contain items 1 and 2, not 3
        assert "bcrypt salt" in result
        assert "validate email format" in result
        assert "HTTPOnly flag" not in result

    def test_single_item_returned_as_is(self):
        result = _extract_actionable_feedback(_BASIC_CRITIQUE_SINGLE_ITEM)
        assert "empty string input" in result

    def test_empty_string_when_no_feedback_section(self):
        result = _extract_actionable_feedback(_BASIC_CRITIQUE_NO_FEEDBACK)
        assert result == ""

    def test_falls_back_to_actionable_mitigations_when_no_feedback(self):
        result = _extract_actionable_feedback(_DIFFUSION_CRITIQUE_WITH_MITIGATIONS)
        assert "rate limiting" in result

    def test_result_capped_at_400_chars(self):
        long_item = "x" * 300
        critique = f"ACTIONABLE FEEDBACK:\n  1. {long_item}\n  2. {long_item}\n"
        result = _extract_actionable_feedback(critique)
        assert len(result) <= 400

    def test_items_joined_by_semicolon(self):
        result = _extract_actionable_feedback(_BASIC_CRITIQUE_WITH_FEEDBACK)
        assert "; " in result

    def test_empty_string_on_empty_input(self):
        assert _extract_actionable_feedback("") == ""


class TestExtractWhatWouldMake10:
    def test_extracts_concrete_sentence(self):
        result = _extract_what_would_make_10(_BASIC_CRITIQUE_WITH_FEEDBACK)
        assert "AuthService" in result

    def test_single_item_critique(self):
        result = _extract_what_would_make_10(_BASIC_CRITIQUE_SINGLE_ITEM)
        assert "edge-case tests" in result

    def test_empty_string_when_section_missing(self):
        result = _extract_what_would_make_10(_BASIC_CRITIQUE_NO_FEEDBACK)
        assert result == ""

    def test_filters_already_perfect_answer(self):
        result = _extract_what_would_make_10(_BASIC_CRITIQUE_ALREADY_PERFECT)
        assert result == ""

    def test_filters_nothing_answer(self):
        critique = "WHAT WOULD MAKE THIS 10/10: nothing more to do"
        assert _extract_what_would_make_10(critique) == ""

    def test_filters_na_answer(self):
        critique = "WHAT WOULD MAKE THIS 10/10: N/A"
        assert _extract_what_would_make_10(critique) == ""

    def test_result_capped_at_200_chars(self):
        long_sentence = "w" * 250
        critique = f"WHAT WOULD MAKE THIS 10/10: {long_sentence}\n"
        result = _extract_what_would_make_10(critique)
        assert len(result) <= 200

    def test_empty_string_on_empty_input(self):
        assert _extract_what_would_make_10("") == ""


class TestMemoryStoreRecordNewFields:
    """Test that record() populates actionable_feedback and what_would_make_10."""

    def test_record_extracts_actionable_feedback(self, tmp_path):
        store = make_store(tmp_path)
        critique = _BASIC_CRITIQUE_WITH_FEEDBACK
        pr = make_phase_result(
            phase_name="impl",
            index=0,
            best_score=7.0,
            basic_critique=critique,
        )
        store.record(outer=1, phase_result=pr)
        entry = store._entries[-1]
        assert "bcrypt salt" in entry.actionable_feedback
        assert "validate email format" in entry.actionable_feedback

    def test_record_extracts_what_would_make_10(self, tmp_path):
        store = make_store(tmp_path)
        critique = _BASIC_CRITIQUE_WITH_FEEDBACK
        pr = make_phase_result(
            phase_name="impl",
            index=0,
            best_score=7.0,
            basic_critique=critique,
        )
        store.record(outer=1, phase_result=pr)
        entry = store._entries[-1]
        assert "AuthService" in entry.what_would_make_10

    def test_record_stores_empty_fields_when_no_sections(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(
            phase_name="impl",
            index=0,
            best_score=9.0,
            basic_critique=_BASIC_CRITIQUE_NO_FEEDBACK,
        )
        store.record(outer=1, phase_result=pr)
        entry = store._entries[-1]
        assert entry.actionable_feedback == ""
        assert entry.what_would_make_10 == ""

    def test_record_filters_already_perfect_what_would_make_10(self, tmp_path):
        store = make_store(tmp_path)
        pr = make_phase_result(
            phase_name="impl",
            index=0,
            best_score=10.0,
            basic_critique=_BASIC_CRITIQUE_ALREADY_PERFECT,
        )
        store.record(outer=1, phase_result=pr)
        entry = store._entries[-1]
        assert entry.what_would_make_10 == ""


class TestMemoryStoreFormatContextNewFields:
    """Test that format_context() renders actionable_feedback and what_would_make_10."""

    def _make_entry_with_feedback(self, round_: int = 1) -> MemoryEntry:
        return MemoryEntry(
            ts="2026-01-01T00:00:00",
            round=round_,
            phase="impl",
            score=7.0,
            score_delta=1.0,
            insight="ok",
            evaluator_top_defect="",
            evaluator_key_risk="",
            actionable_feedback="fix guard; add test",
            what_would_make_10="add strict type check in module.py::func",
        )

    def test_action_items_rendered_in_context(self, tmp_path):
        store = make_store(tmp_path)
        store._entries.append(self._make_entry_with_feedback())
        ctx = store.format_context("impl")
        assert "Action items:" in ctx
        assert "fix guard" in ctx

    def test_to_reach_10_rendered_in_context(self, tmp_path):
        store = make_store(tmp_path)
        store._entries.append(self._make_entry_with_feedback())
        ctx = store.format_context("impl")
        assert "To reach 10/10:" in ctx
        assert "strict type check" in ctx

    def test_empty_fields_not_rendered(self, tmp_path):
        store = make_store(tmp_path)
        entry = MemoryEntry(
            ts="2026-01-01T00:00:00",
            round=1,
            phase="impl",
            score=9.0,
            score_delta=1.0,
            insight="ok",
            evaluator_top_defect="",
            evaluator_key_risk="",
            actionable_feedback="",
            what_would_make_10="",
        )
        store._entries.append(entry)
        ctx = store.format_context("impl")
        assert "Action items:" not in ctx
        assert "To reach 10/10:" not in ctx

    def test_new_fields_survive_jsonl_roundtrip(self, tmp_path):
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="newfields_run")
        store = MemoryStore(artifacts)
        critique = _BASIC_CRITIQUE_WITH_FEEDBACK
        pr = make_phase_result(
            phase_name="impl",
            index=0,
            best_score=7.0,
            basic_critique=critique,
        )
        store.record(outer=1, phase_result=pr)
        # Reload from disk
        store2 = MemoryStore(artifacts)
        assert store2.entry_count == 1
        entry = store2._entries[0]
        assert "bcrypt salt" in entry.actionable_feedback
        assert "AuthService" in entry.what_would_make_10

    def test_old_jsonl_without_new_fields_loads_with_defaults(self, tmp_path):
        """Backward compat: old JSONL lacking actionable_feedback / what_would_make_10."""
        from harness.core.artifacts import ArtifactStore

        artifacts = ArtifactStore(base_dir=tmp_path, run_id="compat_run")
        # Write old-format JSONL directly to the memory file path
        old_line = json.dumps({
            "ts": "2026-01-01T00:00:00",
            "round": 1,
            "phase": "impl",
            "score": 7.0,
            "score_delta": 1.0,
            "insight": "ok",
            "evaluator_top_defect": "bug",
            "evaluator_key_risk": "risk",
            # actionable_feedback and what_would_make_10 intentionally absent
        })
        run_dir = tmp_path / "compat_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "memory.jsonl").write_text(old_line + "\n")
        store = MemoryStore(artifacts)
        assert store.entry_count == 1
        entry = store._entries[0]
        assert entry.actionable_feedback == ""
        assert entry.what_would_make_10 == ""
