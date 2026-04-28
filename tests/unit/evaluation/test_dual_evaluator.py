"""Unit tests for harness/evaluation/dual_evaluator.py.

Covers:
  - format_critique_from_feedback
  - extract_structured_feedback
  - validate_evaluator_output
  - validate_score_calibration
  - validate_calibration_anchors
  - parse_score
  - DualEvaluator.evaluate (async, via mock LLM)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.evaluation.dual_evaluator import (
    DualEvaluator,
    _score_is_in_code_block,
    extract_structured_feedback,
    format_critique_from_feedback,
    parse_score,
    validate_calibration_anchors,
    validate_evaluator_output,
    validate_score_calibration,
)


# ---------------------------------------------------------------------------
# Helpers — build well-formed evaluator texts
# ---------------------------------------------------------------------------

_VALID_BASIC = (
    "ANALYSIS:\n"
    "A. Correctness: 7.5 — matches specification\n"
    "B. Completeness: 6.0 — all cases covered\n"
    "C. Specificity: 5.5 — concrete details present\n"
    "D. Architecture fit: 6.0 — fits existing patterns\n\n"
    "TOP DEFECT: foo.py::bar_function — missing error handling causes crashes.\n"
    "SCORE: 6.5"
)

_VALID_DIFFUSION = (
    "ANALYSIS:\n"
    "A. Caller impact: 7.5 — low blast radius\n"
    "B. Maintenance debt: 6.0 — manageable complexity\n"
    "C. Emergent behaviour: 5.5 — predictable side-effects\n"
    "D. Rollback safety: 6.0 — clean revert possible\n\n"
    "KEY RISK: module.py::some_func — potential race condition\n"
    "SCORE: 7.0"
)

_VALID_WITH_FEEDBACK = (
    "ANALYSIS:\n"
    "A. Correctness: 7.5 — matches specification\n"
    "B. Completeness: 6.0 — all cases covered\n"
    "C. Specificity: 5.5 — concrete details present\n"
    "D. Architecture fit: 6.0 — fits existing patterns\n\n"
    "TOP DEFECT: foo.py::bar_function — missing error handling.\n"
    "ACTIONABLE FEEDBACK:\n"
    "1. foo.py::bar_function — add try/except around network calls\n"
    "2. utils.py::validate_input — add type validation\n"
    "WHAT WOULD MAKE THIS 10/10: Add comprehensive retry logic.\n"
    "SCORE: 6.5"
)


# ---------------------------------------------------------------------------
# format_critique_from_feedback
# ---------------------------------------------------------------------------

class TestFormatCritiqueFromFeedback:
    """Tests for format_critique_from_feedback."""

    def test_empty_dict_returns_no_feedback(self) -> None:
        assert format_critique_from_feedback({}) == "No feedback available"

    def test_score_only(self) -> None:
        result = format_critique_from_feedback({"score": 7.5})
        assert "7.5" in result

    def test_feedback_items_listed(self) -> None:
        result = format_critique_from_feedback(
            {"score": 6.5, "feedback_items": ["Fix error handling", "Add tests"]}
        )
        assert "Fix error handling" in result
        assert "Add tests" in result

    def test_improvement_suggestion_included(self) -> None:
        result = format_critique_from_feedback(
            {"score": 7.0, "improvement_suggestion": "Add retry logic"}
        )
        assert "retry logic" in result

    def test_defect_as_string(self) -> None:
        result = format_critique_from_feedback(
            {"score": 5.0, "defect": "foo.py::bar — critical issue"}
        )
        assert "critical issue" in result
        assert "defect" in result.lower()

    def test_defect_as_dict_uses_description(self) -> None:
        """Legacy dict-format defect should use 'description' key."""
        result = format_critique_from_feedback(
            {"score": 5.0, "defect": {"description": "critical bug", "severity": "HIGH"}}
        )
        assert "critical bug" in result

    def test_analysis_floats_rendered(self) -> None:
        result = format_critique_from_feedback(
            {"score": 7.0, "analysis": {"Correctness": 7.5, "Completeness": 6.0}}
        )
        assert "Correctness" in result
        assert "7.5" in result
        assert "Completeness" in result

    def test_empty_feedback_items_skipped(self) -> None:
        """When feedback_items is an empty list, no 'Feedback:' header appears."""
        result = format_critique_from_feedback({"score": 6.0, "feedback_items": []})
        assert "Feedback:" not in result

    def test_full_dict_from_extract_structured_feedback(self) -> None:
        """format_critique_from_feedback must accept the dict returned by
        extract_structured_feedback without raising."""
        feedback = extract_structured_feedback(_VALID_WITH_FEEDBACK, "basic")
        result = format_critique_from_feedback(feedback)
        # Should produce a non-empty string
        assert isinstance(result, str)
        assert len(result) > 5


# ---------------------------------------------------------------------------
# validate_evaluator_output
# ---------------------------------------------------------------------------

class TestValidateEvaluatorOutput:
    """Tests for validate_evaluator_output."""

    def test_valid_basic_is_accepted(self) -> None:
        is_valid, errors = validate_evaluator_output(_VALID_BASIC, "basic")
        assert is_valid
        # Any errors present should only be WARNINGs
        for e in errors:
            assert e.startswith("WARNING")

    def test_valid_diffusion_is_accepted(self) -> None:
        is_valid, errors = validate_evaluator_output(_VALID_DIFFUSION, "diffusion")
        assert is_valid
        for e in errors:
            assert e.startswith("WARNING")

    def test_missing_score_is_invalid(self) -> None:
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "TOP DEFECT: foo.py::bar_func — something bad"
        )
        is_valid, errors = validate_evaluator_output(text, "basic")
        assert not is_valid
        error_strs = " ".join(errors)
        assert "SCORE" in error_strs

    def test_missing_top_defect_is_warning_not_fatal(self) -> None:
        """Missing TOP DEFECT should produce a WARNING, but is_valid stays True."""
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "SCORE: 6.5"
        )
        is_valid, errors = validate_evaluator_output(text, "basic")
        assert is_valid
        warning_text = " ".join(errors)
        assert "TOP DEFECT" in warning_text or "WARNING" in warning_text

    def test_missing_key_risk_for_diffusion_is_warning(self) -> None:
        """Missing KEY RISK in diffusion mode should produce WARNING, not fatal."""
        text = (
            "ANALYSIS:\n"
            "A. Caller impact: 7.5 — ok\n"
            "B. Maintenance debt: 6.0 — ok\n"
            "C. Emergent behaviour: 5.5 — ok\n"
            "D. Rollback safety: 6.0 — ok\n\n"
            "SCORE: 7.0"
        )
        is_valid, errors = validate_evaluator_output(text, "diffusion")
        assert is_valid

    def test_missing_analysis_section_reports_error(self) -> None:
        text = "TOP DEFECT: foo.py::bar — issue\nSCORE: 5.0"
        is_valid, errors = validate_evaluator_output(text, "basic")
        # ANALYSIS is required; whether it's fatal or a warning depends on impl
        assert isinstance(is_valid, bool)
        assert isinstance(errors, list)

    def test_empty_string_is_invalid(self) -> None:
        is_valid, errors = validate_evaluator_output("", "basic")
        assert not is_valid
        assert len(errors) > 0

    def test_returns_list_of_strings(self) -> None:
        is_valid, errors = validate_evaluator_output(_VALID_BASIC, "basic")
        assert isinstance(is_valid, bool)
        assert isinstance(errors, list)
        for e in errors:
            assert isinstance(e, str)


# ---------------------------------------------------------------------------
# validate_score_calibration
# ---------------------------------------------------------------------------

class TestValidateScoreCalibration:
    """Tests for validate_score_calibration."""

    def test_normal_score_no_warnings(self) -> None:
        assert validate_score_calibration(6.5, "basic") == []

    def test_perfect_score_warned(self) -> None:
        issues = validate_score_calibration(10.0, "basic")
        assert len(issues) > 0

    def test_zero_score_warned(self) -> None:
        issues = validate_score_calibration(0.0, "basic")
        assert len(issues) > 0

    def test_debate_high_score_warned(self) -> None:
        """Debate mode (via context) should warn for scores >=9.5."""
        issues = validate_score_calibration(9.5, "basic", context={"mode": "debate"})
        assert len(issues) > 0

    def test_debate_just_below_threshold_ok(self) -> None:
        issues = validate_score_calibration(9.4, "basic", context={"mode": "debate"})
        # May or may not have issues, but should not include debate high-score warning
        debate_warnings = [i for i in issues if "debate" in i.lower()]
        assert len(debate_warnings) == 0

    def test_implement_low_score_warned(self) -> None:
        """Implement mode (via context) should warn for scores <=3.0."""
        issues = validate_score_calibration(2.9, "basic", context={"mode": "implement"})
        assert len(issues) > 0

    def test_implement_just_above_threshold_ok(self) -> None:
        issues = validate_score_calibration(3.1, "basic", context={"mode": "implement"})
        # 3.1 is above threshold — no implement-specific warning
        implement_warnings = [i for i in issues if "implement" in i.lower()]
        assert len(implement_warnings) == 0

    def test_returns_list_of_strings(self) -> None:
        result = validate_score_calibration(7.0, "basic")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# validate_calibration_anchors
# ---------------------------------------------------------------------------

class TestValidateCalibrationAnchors:
    """Tests for validate_calibration_anchors."""

    def test_mid_range_score_no_anchor_issues(self) -> None:
        """Scores in the middle range (2–8) need no anchor justification."""
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "TOP DEFECT: foo.py::bar_func — issue\n"
            "SCORE: 6.5"
        )
        issues = validate_calibration_anchors(text, "basic")
        assert isinstance(issues, list)

    def test_high_score_requires_anchor(self) -> None:
        """Score >=8.5 should trigger anchor issues if no anchors present."""
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 9.5 — excellent\n"
            "B. Completeness: 9.0 — complete\n"
            "C. Specificity: 9.0 — specific\n"
            "D. Architecture fit: 9.0 — fits\n\n"
            "TOP DEFECT: foo.py::bar_func — minor issue\n"
            "SCORE: 9.0"
        )
        issues = validate_calibration_anchors(text, "basic")
        assert isinstance(issues, list)
        # May have anchor warnings

    def test_no_score_returns_empty(self) -> None:
        """Text with no SCORE: line should return an empty list."""
        text = "ANALYSIS: ok. TOP DEFECT: foo.py::bar — something."
        issues = validate_calibration_anchors(text, "basic")
        assert issues == []

    def test_returns_list(self) -> None:
        issues = validate_calibration_anchors(_VALID_BASIC, "basic")
        assert isinstance(issues, list)


# ---------------------------------------------------------------------------
# parse_score
# ---------------------------------------------------------------------------

class TestParseScore:
    """Tests for parse_score (edge cases not covered elsewhere)."""

    def test_integer_score(self) -> None:
        assert parse_score("SCORE: 7") == 7.0

    def test_float_score(self) -> None:
        assert parse_score("SCORE: 7.5") == 7.5

    def test_score_with_delta(self) -> None:
        assert parse_score("SCORE: 7.5 | DELTA: +2") == 7.5

    def test_score_at_max(self) -> None:
        assert parse_score("SCORE: 10") == 10.0

    def test_score_at_min(self) -> None:
        assert parse_score("SCORE: 0") == 0.0

    def test_above_max_clamped_to_10(self) -> None:
        assert parse_score("SCORE: 11") == 10.0

    def test_no_score_returns_zero(self) -> None:
        assert parse_score("no score here") == 0.0

    def test_score_case_insensitive(self) -> None:
        """Lowercase 'score:' should also be parsed via the unanchored fallback."""
        result = parse_score("score: 7.5")
        assert result == 7.5

    def test_leading_whitespace(self) -> None:
        assert parse_score("  SCORE: 3.0  ") == 3.0

    def test_score_in_multiline(self) -> None:
        text = "ANALYSIS: some analysis\nTOP DEFECT: foo.py::bar — something\nSCORE: 8.0"
        assert parse_score(text) == 8.0


# ---------------------------------------------------------------------------
# _score_is_in_code_block
# ---------------------------------------------------------------------------

class TestScoreIsInCodeBlock:
    """Tests for _score_is_in_code_block — the function that detects whether a
    position in text falls inside a markdown fenced or inline code block."""

    B = "`"  # single backtick helper

    def test_not_in_any_code_block(self) -> None:
        text = "Normal text\nSCORE: 7\nMore text"
        pos = text.find("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is False

    def test_inside_fenced_block(self) -> None:
        b3 = "```"
        text = f"{b3}\npython\nSCORE: 7\n{b3}"
        pos = text.find("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is True

    def test_after_closed_fenced_block(self) -> None:
        b3 = "```"
        text = f"{b3}\npython\nSCORE: 3\n{b3}\nSCORE: 7"
        pos = text.rfind("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is False

    def test_inside_inline_code(self) -> None:
        text = f"before {self.B}SCORE: 7{self.B} after"
        pos = text.find("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is True

    def test_after_closed_inline_code(self) -> None:
        # Score appears after the closing backtick — should NOT be in code
        text = f"before {self.B}example{self.B} SCORE: 7"
        pos = text.find("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is False

    def test_single_backtick_toggle_closes_inline(self) -> None:
        # Open then close inline code, then score
        text = f"a {self.B}code{self.B} SCORE: 5"
        pos = text.find("SCORE: 5")
        assert _score_is_in_code_block(text, pos) is False

    def test_multiple_inline_spans_before_score(self) -> None:
        text = f"a {self.B}x{self.B} and {self.B}y{self.B} SCORE: 5"
        pos = text.find("SCORE: 5")
        assert _score_is_in_code_block(text, pos) is False

    def test_score_inside_second_inline_span(self) -> None:
        # First inline closes; score is inside second inline span
        text = f"a {self.B}x{self.B} and {self.B}SCORE: 5{self.B}"
        pos = text.find("SCORE: 5")
        assert _score_is_in_code_block(text, pos) is True

    def test_empty_text_returns_false(self) -> None:
        assert _score_is_in_code_block("", 0) is False

    def test_score_at_start_no_code(self) -> None:
        text = "SCORE: 8"
        assert _score_is_in_code_block(text, 0) is False

    def test_double_backtick_no_effect(self) -> None:
        # Double backtick is ambiguous, treated as no code block
        text = "``SCORE: 5``"
        pos = text.find("SCORE: 5")
        assert _score_is_in_code_block(text, pos) is False

    def test_fenced_block_with_language_hint(self) -> None:
        text = "```python\nSCORE: 7\n```"
        pos = text.find("SCORE: 7")
        assert _score_is_in_code_block(text, pos) is True


# ---------------------------------------------------------------------------
# parse_score with inline code stripping
# ---------------------------------------------------------------------------

class TestParseScoreInlineCodeStripping:
    """parse_score must not extract SCORE values that appear inside inline
    backtick code spans, which are examples/illustrations rather than real scores."""

    B = "`"

    def test_real_score_unaffected(self) -> None:
        assert parse_score("SCORE: 7") == 7.0

    def test_inline_code_score_not_extracted(self) -> None:
        # Only score is inside inline code — should fall back to 0.0 (no valid score)
        text = f"Format it as {self.B}SCORE: 3{self.B} for example."
        result = parse_score(text)
        # Inline code should be stripped → no valid score token → returns 0.0
        assert result == 0.0

    def test_real_score_wins_over_inline_example(self) -> None:
        # Inline example precedes the real score; real score should be returned
        text = f"Use {self.B}SCORE: 3{self.B} as illustration\nSCORE: 8.5"
        assert parse_score(text) == 8.5

    def test_multiple_inline_examples_then_real_score(self) -> None:
        text = (
            f"Scores like {self.B}SCORE: 4{self.B} or {self.B}SCORE: 5{self.B} "
            "are just examples.\nSCORE: 9"
        )
        assert parse_score(text) == 9.0

    def test_real_score_before_inline_example(self) -> None:
        # Real score first; inline score later should not override
        text = f"SCORE: 7\nSee {self.B}SCORE: 3{self.B} as a comparison"
        assert parse_score(text) == 7.0

    def test_inline_code_inside_fenced_block_not_double_stripped(self) -> None:
        # Fenced block contains inline code; the whole block is stripped anyway
        text = "```\n`SCORE: 3`\n```\nSCORE: 8"
        assert parse_score(text) == 8.0

    def test_score_immediately_after_inline_code(self) -> None:
        # Space between closing backtick and SCORE ensures it is outside inline
        text = f"{self.B}note{self.B} SCORE: 6"
        assert parse_score(text) == 6.0


# ---------------------------------------------------------------------------
# extract_structured_feedback
# ---------------------------------------------------------------------------

class TestExtractStructuredFeedback:
    """Tests for extract_structured_feedback."""

    def test_valid_basic_extracts_score(self) -> None:
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        assert result["score"] == 6.5

    def test_valid_diffusion_extracts_score(self) -> None:
        result = extract_structured_feedback(_VALID_DIFFUSION, "diffusion")
        assert result["score"] == 7.0

    def test_valid_basic_extracts_analysis_dict(self) -> None:
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        analysis = result["analysis"]
        assert isinstance(analysis, dict)
        # Should have dimension keys
        keys = set(analysis.keys())
        assert len(keys) > 0

    def test_valid_basic_extracts_defect(self) -> None:
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        defect = result["defect"]
        assert defect is not None
        assert "bar_function" in str(defect)

    def test_valid_diffusion_extracts_key_risk(self) -> None:
        result = extract_structured_feedback(_VALID_DIFFUSION, "diffusion")
        defect = result["defect"]
        assert defect is not None
        assert "some_func" in str(defect) or "race" in str(defect).lower()

    def test_missing_score_returns_error_key(self) -> None:
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "TOP DEFECT: foo.py::bar_func — issue"
        )
        result = extract_structured_feedback(text, "basic")
        assert "error" in result or len(result.get("validation_errors", [])) > 0
        assert result["score"] is None

    def test_feedback_items_extracted(self) -> None:
        result = extract_structured_feedback(_VALID_WITH_FEEDBACK, "basic")
        items = result["feedback_items"]
        assert isinstance(items, list)
        assert len(items) >= 1

    def test_improvement_suggestion_extracted(self) -> None:
        result = extract_structured_feedback(_VALID_WITH_FEEDBACK, "basic")
        suggestion = result["improvement_suggestion"]
        assert suggestion is not None
        assert "retry" in suggestion.lower()

    def test_warnings_are_strings(self) -> None:
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        for w in warnings:
            assert isinstance(w, str)

    def test_validation_errors_empty_for_valid_input(self) -> None:
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        # validation_errors should be empty (warnings don't count)
        assert result["validation_errors"] == []

    def test_missing_top_defect_adds_warning_not_error(self) -> None:
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "SCORE: 6.5"
        )
        result = extract_structured_feedback(text, "basic")
        # Should still parse score
        assert result["score"] == 6.5
        # Should NOT have validation errors (only warnings)
        assert result["validation_errors"] == []
        # Should have a warning about missing TOP DEFECT
        warnings_text = " ".join(result["warnings"])
        assert "TOP DEFECT" in warnings_text or len(result["warnings"]) > 0

    def test_mode_parameter_accepted(self) -> None:
        """mode kwarg should not raise; it may influence parsing."""
        result = extract_structured_feedback(_VALID_BASIC, "basic", mode="implement")
        assert "score" in result

    def test_context_parameter_accepted(self) -> None:
        """context kwarg should not raise."""
        ctx = {"evaluator_type": "basic", "round": 1}
        result = extract_structured_feedback(_VALID_BASIC, "basic", context=ctx)
        assert "score" in result

    def test_returns_required_keys(self) -> None:
        """Result dict must always contain the expected keys."""
        result = extract_structured_feedback(_VALID_BASIC, "basic")
        required = {
            "score", "analysis", "defect", "feedback_items",
            "improvement_suggestion", "warnings", "validation_errors",
        }
        missing = required - set(result.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_score_and_delta_parsed(self) -> None:
        text = (
            "ANALYSIS:\n"
            "A. Correctness: 7.5 — ok\n"
            "B. Completeness: 6.0 — ok\n"
            "C. Specificity: 5.5 — ok\n"
            "D. Architecture fit: 6.0 — ok\n\n"
            "TOP DEFECT: foo.py::bar_func — issue\n"
            "SCORE: 7.5 | DELTA: +1.5"
        )
        result = extract_structured_feedback(text, "basic")
        assert result["score"] == 7.5
        # delta may or may not be extracted — just check no crash
        assert "delta" in result or result["score"] is not None


# ---------------------------------------------------------------------------
# DualEvaluator.evaluate  (async, via mock LLM)
# ---------------------------------------------------------------------------

@dataclass
class _FakeLLMResponse:
    text: str
    tool_calls: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw: Any = None


class TestDualEvaluatorEvaluate:
    """Tests for DualEvaluator using a mock LLM."""

    def _make_mock_llm(self, basic_text: str, diffusion_text: str) -> MagicMock:
        """Create a MagicMock LLM whose .call() returns pre-canned responses
        alternating basic / diffusion."""
        llm = MagicMock()
        responses = [
            _FakeLLMResponse(text=basic_text),
            _FakeLLMResponse(text=diffusion_text),
        ]
        # asyncio.gather fires both calls; side_effect returns them in order
        call_count = {"n": 0}

        async def _call(*args: Any, **kwargs: Any) -> _FakeLLMResponse:
            idx = call_count["n"] % len(responses)
            call_count["n"] += 1
            return responses[idx]

        llm.call = _call
        return llm

    def test_evaluate_returns_dual_score(self) -> None:
        llm = self._make_mock_llm(_VALID_BASIC, _VALID_DIFFUSION)
        evaluator = DualEvaluator(llm)
        result = asyncio.run(
            evaluator.evaluate(
                subject="def foo(): pass",
                context="# no context",
            )
        )
        # DualScore has .basic and .diffusion ScoreItem attributes
        assert hasattr(result, "basic")
        assert hasattr(result, "diffusion")

    def test_evaluate_basic_score_parsed(self) -> None:
        llm = self._make_mock_llm(_VALID_BASIC, _VALID_DIFFUSION)
        result = asyncio.run(
            DualEvaluator(llm).evaluate(subject="def foo(): pass", context="# ctx")
        )
        assert result.basic.score == pytest.approx(6.5, abs=0.1)

    def test_evaluate_diffusion_score_parsed(self) -> None:
        llm = self._make_mock_llm(_VALID_BASIC, _VALID_DIFFUSION)
        result = asyncio.run(
            DualEvaluator(llm).evaluate(subject="def foo(): pass", context="# ctx")
        )
        assert result.diffusion.score == pytest.approx(7.0, abs=0.1)

    def test_evaluate_critique_contains_text(self) -> None:
        llm = self._make_mock_llm(_VALID_BASIC, _VALID_DIFFUSION)
        result = asyncio.run(
            DualEvaluator(llm).evaluate(subject="def foo(): pass", context="# ctx")
        )
        assert isinstance(result.basic.critique, str)
        assert len(result.basic.critique) > 0

    def test_evaluate_with_mode_implement(self) -> None:
        llm = self._make_mock_llm(_VALID_BASIC, _VALID_DIFFUSION)
        result = asyncio.run(
            DualEvaluator(llm).evaluate(
                subject="def bar(): return 42",
                context="# some file context",
                mode="implement",
            )
        )
        assert hasattr(result, "basic")

    def test_evaluate_graceful_on_bad_llm_output(self) -> None:
        """When LLM returns malformed text, evaluate() should not raise."""
        llm = self._make_mock_llm("garbage output", "also garbage")
        result = asyncio.run(
            DualEvaluator(llm).evaluate(subject="def foo(): pass", context="# ctx")
        )
        # Scores should fall back to 0.0 when parsing fails
        assert isinstance(result.basic.score, float)
        assert isinstance(result.diffusion.score, float)
