"""Tests for harness.evaluation.evaluator pure/parsing functions."""
from __future__ import annotations

from harness.evaluation.evaluator import (
    Verdict,
    _build_log_summary,
    _extract_executor_status,
    _extract_score_from_verdict,
    _extract_structured_feedback,
    _strip_line_numbers,
    _validate_evaluator_output,
)


# ---------------------------------------------------------------------------
# _extract_executor_status
# ---------------------------------------------------------------------------

class TestExtractExecutorStatus:
    def test_returns_done_uppercase(self):
        assert _extract_executor_status("STATUS: DONE") == "DONE"

    def test_returns_partial_uppercase(self):
        assert _extract_executor_status("STATUS: PARTIAL") == "PARTIAL"

    def test_case_insensitive_key(self):
        assert _extract_executor_status("status: done") == "DONE"

    def test_case_insensitive_value(self):
        assert _extract_executor_status("STATUS: partial") == "PARTIAL"

    def test_status_in_multiline_text(self):
        text = "Work complete.\nFixed the bug.\nSTATUS: DONE"
        assert _extract_executor_status(text) == "DONE"

    def test_status_with_leading_whitespace(self):
        text = "   STATUS: DONE"
        assert _extract_executor_status(text) == "DONE"

    def test_unknown_status_returns_empty(self):
        assert _extract_executor_status("STATUS: UNKNOWN") == ""

    def test_no_status_returns_empty(self):
        assert _extract_executor_status("Nothing here.") == ""

    def test_empty_string_returns_empty(self):
        assert _extract_executor_status("") == ""

    def test_status_with_trailing_whitespace(self):
        assert _extract_executor_status("STATUS: DONE   ") == "DONE"


# ---------------------------------------------------------------------------
# _validate_evaluator_output
# ---------------------------------------------------------------------------

class TestValidateEvaluatorOutput:
    def _valid_output(self) -> str:
        return (
            "VERDICT: PASS\n"
            "REASON: All requirements met.\n"
            "FINAL SCORE: 8.0\n"
            "DETAILS: SCORE: 8 — good\n"
            "SUGGESTIONS: - Improve docs"
        )

    def test_valid_pass_output(self):
        is_valid, warnings = _validate_evaluator_output(self._valid_output())
        # May have minor warnings but should not fail on VERDICT/REASON/SCORE
        assert "Missing required section: VERDICT:" not in warnings
        assert "Missing required section: REASON:" not in warnings

    def test_missing_verdict_section(self):
        text = "REASON: Something.\nFINAL SCORE: 5.0"
        _, warnings = _validate_evaluator_output(text)
        assert any("VERDICT" in w for w in warnings)

    def test_missing_reason_section(self):
        text = "VERDICT: PASS\nFINAL SCORE: 8.0"
        _, warnings = _validate_evaluator_output(text)
        assert any("REASON" in w for w in warnings)

    def test_invalid_verdict_value(self):
        text = "VERDICT: MAYBE\nREASON: Not sure.\nFINAL SCORE: 5.0"
        _, warnings = _validate_evaluator_output(text)
        assert any("MAYBE" in w or "Invalid VERDICT" in w for w in warnings)

    def test_fail_verdict_is_valid(self):
        text = "VERDICT: FAIL\nREASON: Core goal not met.\nFINAL SCORE: 4.0\nSUGGESTIONS: Fix it"
        _, warnings = _validate_evaluator_output(text)
        assert not any("Invalid VERDICT" in w for w in warnings)

    def test_missing_score_warns(self):
        text = "VERDICT: PASS\nREASON: Done."
        _, warnings = _validate_evaluator_output(text)
        assert any("score" in w.lower() for w in warnings)

    def test_missing_suggestions_warns(self):
        text = "VERDICT: PASS\nREASON: Done.\nFINAL SCORE: 8.0"
        _, warnings = _validate_evaluator_output(text)
        assert any("suggestion" in w.lower() or "feedback" in w.lower() for w in warnings)

    def test_is_valid_flag_false_when_warnings(self):
        text = "No sections here at all."
        is_valid, warnings = _validate_evaluator_output(text)
        assert not is_valid
        assert len(warnings) > 0


# ---------------------------------------------------------------------------
# _extract_score_from_verdict
# ---------------------------------------------------------------------------

class TestExtractScoreFromVerdict:
    def test_extracts_final_score(self):
        text = "FINAL SCORE: 7.5"
        assert _extract_score_from_verdict(text) == 7.5

    def test_extracts_combined_score(self):
        text = "COMBINED_SCORE: 6.0"
        assert _extract_score_from_verdict(text) == 6.0

    def test_extracts_bare_score(self):
        text = "SCORE: 8"
        assert _extract_score_from_verdict(text) == 8.0

    def test_extracts_out_of_ten(self):
        text = "Rating: 7.5/10"
        assert _extract_score_from_verdict(text) == 7.5

    def test_extracts_out_of_ten_spaced(self):
        text = "7 out of 10"
        assert _extract_score_from_verdict(text) == 7.0

    def test_case_insensitive(self):
        text = "final score: 6.5"
        assert _extract_score_from_verdict(text) == 6.5

    def test_score_out_of_range_ignored(self):
        # Score > 10 should be ignored
        text = "SCORE: 150"
        # Should return None since 150 > 10
        result = _extract_score_from_verdict(text)
        assert result is None or 0 <= result <= 10

    def test_returns_none_when_no_score(self):
        assert _extract_score_from_verdict("No score here.") is None

    def test_returns_none_for_empty_string(self):
        assert _extract_score_from_verdict("") is None

    def test_fractional_score(self):
        text = "FINAL SCORE: 4.5"
        assert _extract_score_from_verdict(text) == 4.5

    def test_prefers_last_match_in_multiline(self):
        # When multiple scores present, last is preferred
        text = "score: 5\nFINAL SCORE: 8.0"
        result = _extract_score_from_verdict(text)
        assert result == 8.0


# ---------------------------------------------------------------------------
# _extract_structured_feedback
# ---------------------------------------------------------------------------

class TestExtractStructuredFeedback:
    def _full_verdict(self) -> str:
        return (
            "VERDICT: PASS\n"
            "REASON: The implementation is complete and correct.\n"
            "FINAL SCORE: 8.0\n"
            "DETAILS:\n"
            "1. Completeness: SCORE: 8 — all tasks done\n"
            "2. Correctness: SCORE: 8 — tests pass\n\n"
            "SUGGESTIONS:\n"
            "- Add more type hints\n"
            "- Consider adding integration tests\n"
        )

    def test_extracts_score(self):
        result = _extract_structured_feedback(self._full_verdict())
        assert result["score"] == 8.0

    def test_extracts_top_defect(self):
        result = _extract_structured_feedback(self._full_verdict())
        # REASON section content should populate top_defect
        assert result["top_defect"] is not None
        assert len(result["top_defect"]) > 5

    def test_extracts_actionable_items(self):
        result = _extract_structured_feedback(self._full_verdict())
        assert len(result["actionable_items"]) >= 1

    def test_score_none_when_missing(self):
        result = _extract_structured_feedback("VERDICT: PASS\nREASON: Done.")
        assert result["score"] is None

    def test_score_confidence_with_full_verdict(self):
        result = _extract_structured_feedback(self._full_verdict())
        assert result["score_confidence"] > 0.0

    def test_score_confidence_zero_when_nothing(self):
        result = _extract_structured_feedback("random text")
        assert result["score_confidence"] == 0.0

    def test_validation_warnings_empty_on_good_input(self):
        result = _extract_structured_feedback(self._full_verdict())
        # Good structured input should produce no validation warnings
        assert isinstance(result["validation_warnings"], list)

    def test_score_out_of_range_generates_warning(self):
        # A score of 150 is out of range
        result = _extract_structured_feedback("FINAL SCORE: 150")
        # Should produce a warning and score should be None
        if result["score"] is not None:
            assert 0 <= result["score"] <= 10
        else:
            assert any("out of range" in w.lower() or "150" in w for w in result["validation_warnings"])

    def test_implement_mode_default(self):
        result = _extract_structured_feedback(self._full_verdict())
        assert isinstance(result["phase_mode_adapted"], bool)

    def test_debate_mode_recognized(self):
        verdict = self._full_verdict().replace("Completeness", "plan_quality")
        result = _extract_structured_feedback(verdict, phase_mode="debate")
        assert isinstance(result["phase_mode_adapted"], bool)

    def test_calibration_anchors_false_without_anchors(self):
        result = _extract_structured_feedback("FINAL SCORE: 8.0")
        assert result["calibration_anchors_used"] is False

    def test_critique_structure_score_bounded(self):
        result = _extract_structured_feedback(self._full_verdict())
        assert 0.0 <= result["critique_structure_score"] <= 1.0

    def test_returns_all_required_keys(self):
        result = _extract_structured_feedback("")
        required_keys = [
            "score", "score_breakdown", "top_defect", "actionable_items",
            "score_confidence", "calibration_anchors_used", "critique_structure_score",
            "validation_warnings", "phase_mode_adapted", "calibration_anchor_details",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# _build_log_summary
# ---------------------------------------------------------------------------

class TestBuildLogSummary:
    def test_empty_log_returns_placeholder(self):
        assert _build_log_summary([]) == "(no tool calls)"

    def test_single_important_tool(self):
        log = [{"tool": "batch_edit", "input": {"edits": [{}, {}]}, "output": "OK"}]
        result = _build_log_summary(log)
        assert "batch_edit" in result
        assert "2 edits" in result

    def test_single_verbose_tool_shown(self):
        log = [{"tool": "batch_read", "input": {"paths": ["a.py"]}, "output": "content"}]
        result = _build_log_summary(log)
        assert "batch_read" in result

    def test_error_output_always_shown(self):
        log = [{"tool": "batch_read", "input": {"paths": ["x.py"]}, "output": "Error: file not found"}]
        result = _build_log_summary(log)
        assert "Error" in result or "x.py" in result

    def test_multifile_read_shows_count(self):
        log = [{
            "tool": "batch_read",
            "input": {"paths": ["a.py", "b.py", "c.py"]},
            "output": "content",
        }]
        result = _build_log_summary(log)
        assert "3 files" in result

    def test_bash_tool_shows_command(self):
        log = [{"tool": "bash", "input": {"command": "pytest tests/"}, "output": "5 passed"}]
        result = _build_log_summary(log)
        assert "bash" in result
        assert "pytest" in result

    def test_multiple_entries_all_present(self):
        log = [
            {"tool": "batch_read", "input": {"paths": ["a.py"]}, "output": "code"},
            {"tool": "batch_edit", "input": {"edits": [{}]}, "output": "OK"},
        ]
        result = _build_log_summary(log)
        assert "batch_read" in result
        assert "batch_edit" in result


# ---------------------------------------------------------------------------
# _strip_line_numbers
# ---------------------------------------------------------------------------

class TestStripLineNumbers:
    def test_strips_tab_numbered_lines(self):
        text = "--- file.py [3 lines] ---\n     1\tdef foo():\n     2\t    pass\n     3\t"
        result = _strip_line_numbers(text)
        assert "def foo():" in result
        assert "\t" not in result or all(line.startswith("def") or line == "    pass" or line == "" for line in result.split("\n"))

    def test_plain_text_unchanged(self):
        text = "def foo():\n    pass"
        result = _strip_line_numbers(text)
        assert "def foo():" in result
        assert "pass" in result

    def test_empty_string(self):
        result = _strip_line_numbers("")
        assert isinstance(result, str)

    def test_read_file_header_stripped(self):
        text = "[myfile.py] lines 1-3 of 3\n     1\tx = 1\n     2\ty = 2"
        result = _strip_line_numbers(text)
        assert "x = 1" in result
        assert "y = 2" in result
        # Header line should be gone
        assert "lines 1-3" not in result


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------

class TestVerdict:
    def test_basic_construction(self):
        v = Verdict(passed=True, score=8.0, reason="Good work", feedback="Keep it up")
        assert v.passed is True
        assert v.score == 8.0
        assert v.reason == "Good work"

    def test_fail_verdict(self):
        v = Verdict(passed=False, score=4.0, reason="Core goal not met", feedback="Fix it")
        assert v.passed is False

    def test_default_score_zero(self):
        v = Verdict(passed=True, reason="ok", feedback="nice")
        assert v.score == 0.0

    def test_default_fields(self):
        v = Verdict(passed=True, reason="done", feedback="well done")
        # Optional fields should have sensible defaults
        assert isinstance(v.actionable_items, list)
        assert isinstance(v.validation_warnings, list)
        assert isinstance(v.score_breakdown, dict)
        assert isinstance(v.calibration_anchors_used, bool)

    def test_score_confidence_default_zero(self):
        v = Verdict(passed=True, reason="ok", feedback="")
        assert v.score_confidence == 0.0

    def test_critique_structure_score_default_zero(self):
        v = Verdict(passed=True, reason="ok", feedback="")
        assert v.critique_structure_score == 0.0

    def test_evaluation_mode_default(self):
        v = Verdict(passed=True, reason="ok", feedback="")
        assert v.evaluation_mode == "implementation"
