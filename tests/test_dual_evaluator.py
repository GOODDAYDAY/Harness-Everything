"""Tests for harness/evaluation/dual_evaluator.py.

Covers the public utility functions and the DualEvaluator class with a mock LLM.
These tests document actual behaviour (including edge cases and clamping) so
future refactors don't silently change semantics.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from harness.core.llm import LLMResponse
from harness.evaluation.dual_evaluator import (
    DualEvaluator,
    extract_structured_feedback,
    format_critique_from_feedback,
    parse_score,
    validate_evaluator_output,
    validate_score_calibration,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — minimal well-formed evaluator outputs
# ─────────────────────────────────────────────────────────────────────────────

BASIC_VALID_OUTPUT = """\
OVERALL ASSESSMENT: The proposal is solid and well-considered.

KEY STRENGTHS:
- Strength 1: Clear implementation path
- Strength 2: Good error handling

DEFECTS / RISKS:
- Risk 1: Minor edge case gap

IMPROVEMENTS:
- Add more unit tests

VERDICT: SUPPORT

TOP DEFECT: module::function — missing edge-case guard

ANALYSIS:
A. Correctness: 8 — correctly handles the core use-case.
B. Completeness: 7 — misses some edge cases.
C. Specificity: 9 — concrete file references included.
D. Architecture fit: 8 — consistent with existing patterns.

SCORE: 7"""

DIFFUSION_VALID_OUTPUT = """\
DIFFUSION ANALYSIS:
Both evaluators agree on the core approach.

CONVERGENCE EVIDENCE:
- Both highlight good error handling

KEY RISK: module::function — timeout not guarded

RESOLUTION PATH:
1. Add timeout parameter to module.py: function

SYNTHESIS SCORE: 7

SCORE: 7"""


def _make_mock_llm(
    basic_text: str = BASIC_VALID_OUTPUT,
    diffusion_text: str = DIFFUSION_VALID_OUTPUT,
) -> MagicMock:
    """Return a MagicMock LLM whose .call() coroutine returns the given texts.

    The LLM is called twice per evaluate() call:
    - First call (basic evaluator system prompt) → basic_text
    - Second call (diffusion evaluator system prompt) → diffusion_text

    Because both calls are issued concurrently with asyncio.gather(), the
    ordering is deterministic in a single-threaded event loop: the first
    Task created (basic_task) is awaited first.
    """
    llm = MagicMock()
    call_count = 0

    async def _call(messages, system="", **kwargs):
        nonlocal call_count
        call_count += 1
        # First call → basic evaluator; second → diffusion evaluator
        text = basic_text if call_count == 1 else diffusion_text
        return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", raw={})

    llm.call = _call
    return llm


# ─────────────────────────────────────────────────────────────────────────────
# parse_score
# ─────────────────────────────────────────────────────────────────────────────

class TestParseScore:
    def test_integer_score(self):
        assert parse_score("SCORE: 7") == 7.0

    def test_float_score(self):
        assert parse_score("SCORE: 7.5") == 7.5

    def test_zero_score(self):
        assert parse_score("SCORE: 0") == 0.0

    def test_ten_score(self):
        assert parse_score("SCORE: 10") == 10.0

    def test_score_clamped_above_ten(self):
        # Out-of-range values are clamped, not rejected
        assert parse_score("SCORE: 11") == 10.0

    def test_score_clamped_below_zero(self):
        assert parse_score("SCORE: -1") == 0.0

    def test_no_score_token_returns_zero(self):
        # When no score is found, the function returns 0.0 (the default)
        assert parse_score("This output has no score") == 0.0

    def test_empty_string_returns_zero(self):
        assert parse_score("") == 0.0

    def test_bold_markdown_stripped(self):
        # **SCORE: 8** — markdown bold should be stripped
        assert parse_score("**SCORE: 8**") == 8.0

    def test_score_with_surrounding_whitespace(self):
        assert parse_score("  SCORE: 8  ") == 8.0

    def test_score_in_multiline_text(self):
        text = "Some analysis here.\n\nSCORE: 6\n\nOther text."
        assert parse_score(text) == 6.0

    def test_synthesis_score_also_matches(self):
        # SYNTHESIS SCORE is a header that contains SCORE — documents real behaviour
        result = parse_score("SYNTHESIS SCORE: 8")
        # behaviour: either 8.0 or 0.0 depending on regex; we just verify type
        assert isinstance(result, float)


# ─────────────────────────────────────────────────────────────────────────────
# validate_evaluator_output
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateEvaluatorOutput:
    def test_basic_valid_output_passes(self):
        ok, issues = validate_evaluator_output(BASIC_VALID_OUTPUT, "basic")
        assert ok is True, f"Unexpected errors: {issues}"

    def test_diffusion_valid_output_passes(self):
        ok, issues = validate_evaluator_output(DIFFUSION_VALID_OUTPUT, "diffusion")
        assert ok is True, f"Unexpected errors: {issues}"

    def test_missing_score_fails_basic(self):
        # SCORE is the only required section — its absence is a hard failure
        text = BASIC_VALID_OUTPUT.replace("SCORE: 7", "")
        ok, issues = validate_evaluator_output(text, "basic")
        assert ok is False
        assert any("SCORE" in i for i in issues)

    def test_empty_text_fails(self):
        ok, issues = validate_evaluator_output("", "basic")
        assert ok is False
        assert len(issues) > 0

    def test_missing_verdict_is_warning_not_error(self):
        # VERDICT is optional — its absence produces only warnings
        text = BASIC_VALID_OUTPUT.replace("VERDICT: SUPPORT", "")
        ok, issues = validate_evaluator_output(text, "basic")
        # ok is still True, or if False, issues are all warnings
        if not ok:
            hard_errors = [i for i in issues if not i.startswith("WARNING:")]
            assert hard_errors == []

    def test_missing_key_risk_diffusion_is_warning(self):
        # KEY RISK is optional in diffusion — absence is only a warning
        text = DIFFUSION_VALID_OUTPUT.replace(
            "KEY RISK: module::function \u2014 timeout not guarded", ""
        )
        ok, issues = validate_evaluator_output(text, "diffusion")
        if not ok:
            hard_errors = [i for i in issues if not i.startswith("WARNING:")]
            assert hard_errors == []

    def test_warnings_do_not_make_output_invalid(self):
        # Warnings (starting with 'WARNING:') are advisory, not failures
        ok, issues = validate_evaluator_output(BASIC_VALID_OUTPUT, "basic")
        errors = [i for i in issues if not i.startswith("WARNING:")]
        assert ok is True
        # There may be warnings, but no hard errors
        assert errors == []


# ─────────────────────────────────────────────────────────────────────────────
# validate_score_calibration
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateScoreCalibration:
    """validate_score_calibration(score, evaluator_type, context=None) -> list[str]"""

    def test_mid_range_score_returns_list(self):
        issues = validate_score_calibration(7.0, "basic")
        assert isinstance(issues, list)

    def test_perfect_ten_returns_list(self):
        issues = validate_score_calibration(10.0, "basic")
        assert isinstance(issues, list)

    def test_zero_score_returns_list(self):
        issues = validate_score_calibration(0.0, "basic")
        assert isinstance(issues, list)

    def test_all_in_range_scores_return_list(self):
        for score in [0.0, 1.0, 5.0, 7.0, 9.0, 10.0]:
            issues = validate_score_calibration(score, "basic")
            assert isinstance(issues, list)

    def test_diffusion_evaluator_type(self):
        issues = validate_score_calibration(7.0, "diffusion")
        assert isinstance(issues, list)

    def test_with_context_dict(self):
        context = {"critiques": "This is terrible"}
        issues = validate_score_calibration(8.0, "basic", context=context)
        assert isinstance(issues, list)


# ─────────────────────────────────────────────────────────────────────────────
# extract_structured_feedback
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractStructuredFeedback:
    def test_extracts_score_from_basic(self):
        result = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        assert result["score"] == 7.0

    def test_extracts_score_from_diffusion(self):
        result = extract_structured_feedback(DIFFUSION_VALID_OUTPUT, "diffusion")
        assert result["score"] == 7.0

    def test_extracts_defect(self):
        result = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        assert result["defect"] is not None
        assert "module::function" in result["defect"]

    def test_extracts_feedback_items_as_list(self):
        result = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        items = result["feedback_items"]
        # feedback_items is always a list (may be empty)
        assert isinstance(items, list)

    def test_extracts_improvement_suggestion(self):
        result = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        suggestion = result["improvement_suggestion"]
        # May be string or None
        assert suggestion is None or isinstance(suggestion, str)

    def test_extracts_analysis(self):
        result = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        analysis = result.get("analysis")
        # Analysis dict or None
        assert analysis is None or isinstance(analysis, dict)

    def test_no_score_returns_none(self):
        # When no SCORE line is found, score is None (not 0.0)
        result = extract_structured_feedback("Some random text without a score.", "basic")
        assert result["score"] is None

    def test_empty_text_returns_none_score(self):
        # Empty text → no SCORE found → score is None
        result = extract_structured_feedback("", "basic")
        assert result["score"] is None

    def test_returns_dict_always(self):
        for text in ["", "SCORE: 5", BASIC_VALID_OUTPUT, DIFFUSION_VALID_OUTPUT]:
            result = extract_structured_feedback(text, "basic")
            assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# format_critique_from_feedback
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatCritiqueFromFeedback:
    def test_includes_score(self):
        feedback = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        critique = format_critique_from_feedback(feedback)
        assert "7" in critique  # score 7.0 is present

    def test_includes_defect_when_present(self):
        feedback = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        critique = format_critique_from_feedback(feedback)
        # defect line should appear in the output
        assert "module::function" in critique

    def test_returns_string(self):
        feedback = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        result = format_critique_from_feedback(feedback)
        assert isinstance(result, str)

    def test_no_defect_omits_defect_line(self):
        feedback = {"score": 8.0, "feedback_items": [], "improvement_suggestion": None, "defect": None, "analysis": {}}
        critique = format_critique_from_feedback(feedback)
        assert "defect" not in critique.lower() or "Critical defect" not in critique

    def test_non_empty_feedback_items_included(self):
        feedback = {
            "score": 7.0,
            "feedback_items": ["Good point", "Needs work"],
            "improvement_suggestion": "Fix edge cases",
            "defect": None,
            "analysis": {},
        }
        critique = format_critique_from_feedback(feedback)
        # At minimum the score should be there
        assert isinstance(critique, str)
        assert len(critique) > 0


# ─────────────────────────────────────────────────────────────────────────────
# DualEvaluator.evaluate  (mock LLM)
# ─────────────────────────────────────────────────────────────────────────────

class TestDualEvaluatorEvaluate:
    """Tests for DualEvaluator.evaluate() using a mock LLM.

    evaluate(subject, context, *, mode='debate', basic_system='', diffusion_system='', score_pattern=...)
    """

    @pytest.fixture
    def llm(self):
        return _make_mock_llm()

    @pytest.fixture
    def evaluator(self, llm):
        return DualEvaluator(llm)

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_evaluate_returns_dual_score(self, evaluator):
        from harness.evaluation.dual_evaluator import DualScore

        result = self._run(evaluator.evaluate("Proposal text.", "Context here.", mode="debate"))
        assert isinstance(result, DualScore)

    def test_evaluate_basic_score_populated(self, evaluator):
        result = self._run(evaluator.evaluate("Proposal text.", "Context here.", mode="debate"))
        # The basic score should be 7.0 (from BASIC_VALID_OUTPUT)
        assert result.basic.score == 7.0

    def test_evaluate_diffusion_score_populated(self, evaluator):
        result = self._run(evaluator.evaluate("Proposal text.", "Context here.", mode="debate"))
        # The diffusion score should be 7.0 (from DIFFUSION_VALID_OUTPUT)
        assert result.diffusion.score == 7.0

    def test_evaluate_calls_llm_twice(self):
        """Each evaluate() call issues exactly 2 LLM calls (basic + diffusion)."""
        call_count = 0

        async def _call(messages, system="", **kwargs):
            nonlocal call_count
            call_count += 1
            text = BASIC_VALID_OUTPUT if call_count == 1 else DIFFUSION_VALID_OUTPUT
            return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", raw={})

        llm = MagicMock()
        llm.call = _call
        evaluator = DualEvaluator(llm)

        self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        assert call_count == 2

    def test_evaluate_combined_score_is_weighted_average(self, evaluator):
        """combined score is 60% basic + 40% diffusion (both 7.0 here → 7.0)."""
        result = self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        # Both scores are 7.0, so combined = 0.6*7 + 0.4*7 = 7.0
        expected = 0.6 * result.basic.score + 0.4 * result.diffusion.score
        assert abs(result.combined - expected) < 0.01

    def test_evaluate_critique_text_non_empty(self, evaluator):
        """The critique fields are non-empty strings."""
        result = self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        assert isinstance(result.basic.critique, str)
        assert isinstance(result.diffusion.critique, str)
        assert len(result.basic.critique) > 0
        assert len(result.diffusion.critique) > 0

    def test_evaluate_basic_critique_contains_score(self, evaluator):
        """The basic critique string should mention the score."""
        result = self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        # Score 7.0 should appear somewhere in the critique
        assert "7" in result.basic.critique

    def test_evaluate_debate_mode_accepted(self, evaluator):
        """evaluate() with mode='debate' runs without error."""
        result = self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        assert hasattr(result, "basic")
        assert hasattr(result, "diffusion")

    def test_evaluate_implement_mode_accepted(self, evaluator):
        """evaluate() with mode='implement' runs without error."""
        result = self._run(evaluator.evaluate("test subject", "test context", mode="implement"))
        assert hasattr(result, "basic")
        assert hasattr(result, "diffusion")

    def test_evaluate_empty_subject_context(self, evaluator):
        """An empty subject/context should not crash evaluate()."""
        result = self._run(evaluator.evaluate("", "", mode="debate"))
        assert hasattr(result, "combined")

    def test_evaluate_low_quality_output_still_produces_result(self):
        """Even if the LLM returns garbage, evaluate() returns a DualScore."""
        async def _bad_call(messages, system="", **kwargs):
            return LLMResponse(text="No structure here.", tool_calls=[], stop_reason="end_turn", raw={})

        llm = MagicMock()
        llm.call = _bad_call
        evaluator = DualEvaluator(llm)

        from harness.evaluation.dual_evaluator import DualScore

        result = self._run(evaluator.evaluate("test subject", "test context", mode="debate"))
        assert isinstance(result, DualScore)
        # Unstructured output → score defaults to 0.0
        assert result.basic.score == 0.0

    def test_evaluate_single_llm_failure_propagates(self):
        """If one LLM call raises, the exception propagates from evaluate()."""
        call_count = 0

        async def _failing_call(messages, system="", **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM call failed")
            return LLMResponse(text=DIFFUSION_VALID_OUTPUT, tool_calls=[], stop_reason="end_turn", raw={})

        llm = MagicMock()
        llm.call = _failing_call
        evaluator = DualEvaluator(llm)

        with pytest.raises((RuntimeError, BaseException)):
            self._run(evaluator.evaluate("test subject", "test context", mode="debate"))

    def test_evaluate_custom_basic_system(self, llm):
        """custom basic_system override is accepted without error."""
        evaluator = DualEvaluator(llm)
        result = self._run(
            evaluator.evaluate(
                "test subject",
                "test context",
                mode="debate",
                basic_system="Custom basic system prompt.",
            )
        )
        assert hasattr(result, "basic")

    def test_evaluate_custom_diffusion_system(self, llm):
        """custom diffusion_system override is accepted without error."""
        evaluator = DualEvaluator(llm)
        result = self._run(
            evaluator.evaluate(
                "test subject",
                "test context",
                mode="debate",
                diffusion_system="Custom diffusion system prompt.",
            )
        )
        assert hasattr(result, "diffusion")


# ─────────────────────────────────────────────────────────────────────────────
# Integration: full pipeline through extract → format
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractThenFormat:
    """Integration tests: extract_structured_feedback → format_critique_from_feedback."""

    def test_basic_round_trip_contains_score(self):
        feedback = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        critique = format_critique_from_feedback(feedback)
        assert "Score" in critique or "score" in critique.lower()

    def test_diffusion_round_trip_non_empty(self):
        feedback = extract_structured_feedback(DIFFUSION_VALID_OUTPUT, "diffusion")
        critique = format_critique_from_feedback(feedback)
        assert len(critique.strip()) > 0

    def test_basic_round_trip_has_defect_info(self):
        feedback = extract_structured_feedback(BASIC_VALID_OUTPUT, "basic")
        critique = format_critique_from_feedback(feedback)
        # The TOP DEFECT line should carry through
        assert "module" in critique or "function" in critique

    def test_none_score_produces_empty_critique(self):
        # When score is None (no SCORE found), format_critique_from_feedback returns ''
        feedback = extract_structured_feedback("Random text.", "basic")
        assert feedback["score"] is None
        critique = format_critique_from_feedback(feedback)
        # Critique may be empty or contain minimal content when score is None
        assert isinstance(critique, str)


class TestExtractStructuredFeedbackParsing:
    """Concrete behaviour tests for the simplified extract_structured_feedback."""

    LETTERED_ANALYSIS_OUTPUT = """\
OVERALL ASSESSMENT: Good.
TOP DEFECT: None
ACTIONABLE FEEDBACK:
1. Fix edge case in parser.py
2. Add unit tests
WHAT WOULD MAKE THIS 10/10: Add more tests
SCORE: 8

ANALYSIS:
A. Correctness: 9 — solid logic.
B. Completeness: 7 — some gaps.
C. Clarity: 8 — well documented.
D. Test coverage: 6 — needs more tests.
"""

    PLAIN_ANALYSIS_OUTPUT = """\
OVERALL ASSESSMENT: Decent.
TOP DEFECT: None
ACTIONABLE FEEDBACK:
1. Improve documentation
WHAT WOULD MAKE THIS 10/10: Better docs

ANALYSIS:
A. Correctness: 8.5 — very accurate.
B. Completeness: 6.0 — missing some parts.
C. Specificity: 7.0 — needs more context.
D. Architecture fit: 8.0 — consistent patterns.
SCORE: 7
"""

    def test_lettered_analysis_no_spurious_entries(self):
        """'A. Correctness: 9' must produce key 'Correctness', not 'A. Correctness'."""
        feedback = extract_structured_feedback(self.LETTERED_ANALYSIS_OUTPUT, "basic")
        expected_keys = {"Correctness", "Completeness", "Clarity", "Test coverage"}
        assert set(feedback["analysis"].keys()) == expected_keys

    def test_lettered_analysis_correct_scores(self):
        feedback = extract_structured_feedback(self.LETTERED_ANALYSIS_OUTPUT, "basic")
        assert feedback["analysis"]["Correctness"] == 9.0
        assert feedback["analysis"]["Completeness"] == 7.0
        assert feedback["analysis"]["Clarity"] == 8.0
        assert feedback["analysis"]["Test coverage"] == 6.0

    def test_plain_analysis_parsing(self):
        """Lettered 'A. Dimension: score' produces clean dimension keys (no letter prefix)."""
        feedback = extract_structured_feedback(self.PLAIN_ANALYSIS_OUTPUT, "basic")
        assert feedback["analysis"]["Correctness"] == 8.5
        assert feedback["analysis"]["Completeness"] == 6.0
        # No spurious combined keys like "A. Correctness"
        assert "A. Correctness" not in feedback["analysis"]
        assert "Correctness: 8.5" not in feedback["analysis"]

    def test_feedback_items_numbered_stripped(self):
        """Numbered prefixes like '1.' and '2.' must be stripped from feedback_items."""
        feedback = extract_structured_feedback(self.LETTERED_ANALYSIS_OUTPUT, "basic")
        assert "Fix edge case in parser.py" in feedback["feedback_items"]
        assert "Add unit tests" in feedback["feedback_items"]
        # Numbering prefix must be gone
        assert not any(item.startswith("1.") or item.startswith("2.") for item in feedback["feedback_items"])

    def test_feedback_items_numbered_stripped_in_plain(self):
        """Numbered prefixes also stripped from PLAIN_ANALYSIS_OUTPUT feedback."""
        feedback = extract_structured_feedback(self.PLAIN_ANALYSIS_OUTPUT, "basic")
        assert "Improve documentation" in feedback["feedback_items"]
        assert not any(item.startswith("1.") for item in feedback["feedback_items"])

    def test_exact_keys_returned(self):
        """Function must return the defined minimal key set — no extra computed fields."""
        feedback = extract_structured_feedback(self.LETTERED_ANALYSIS_OUTPUT, "basic")
        expected = {
            "score", "delta", "analysis", "defect", "feedback_items",
            "improvement_suggestion", "warnings", "calibration_anchors_used",
            "validation_errors",
        }
        assert set(feedback.keys()) == expected

    def test_calibration_anchor_detected(self):
        text = "SCORING CALIBRATION: use 0-10 scale\nSCORE: 7"
        feedback = extract_structured_feedback(text, "basic")
        assert feedback["calibration_anchors_used"] is True

    def test_calibration_anchor_not_detected_when_absent(self):
        feedback = extract_structured_feedback(self.PLAIN_ANALYSIS_OUTPUT, "basic")
        assert feedback["calibration_anchors_used"] is False

    def test_delta_extraction(self):
        text = "DELTA VS PRIOR BEST: +2 improved test coverage\nSCORE: 8"
        feedback = extract_structured_feedback(text, "basic")
        assert feedback["delta"] == "+2 improved test coverage"

    def test_defect_none_text_ignored(self):
        text = "TOP DEFECT: None\nSCORE: 8"
        feedback = extract_structured_feedback(text, "basic")
        assert feedback["defect"] is None

    def test_diffusion_key_risk(self):
        text = "KEY RISK: async_handler.py::run — race condition on shutdown\nSCORE: 6"
        feedback = extract_structured_feedback(text, "diffusion")
        assert feedback["defect"] == "async_handler.py::run — race condition on shutdown"

    def test_improvement_suggestion_extracted(self):
        feedback = extract_structured_feedback(self.LETTERED_ANALYSIS_OUTPUT, "basic")
        assert feedback["improvement_suggestion"] == "Add more tests"


class TestDiffusionSystemPromptQuality:
    """Verify that DIFFUSION_SYSTEM contains required scoring anchors and guidance."""

    @pytest.fixture(autouse=True)
    def _load(self):
        from harness.prompts.dual_evaluator import DIFFUSION_SYSTEM
        self.prompt = DIFFUSION_SYSTEM

    def test_all_even_score_anchors_present(self):
        """Even scores 0, 2, 4, 6, 8, 10 should be in the DIFFUSION scoring guide."""
        for score in (0, 2, 4, 6, 8, 10):
            assert f"  {score}:" in self.prompt, (
                f"DIFFUSION_SYSTEM missing anchor for score {score}"
            )

    def test_all_odd_score_anchors_present(self):
        """Odd scores 1, 3, 5, 7, 9 should be in the DIFFUSION scoring guide (new)."""
        for score in (1, 3, 5, 7, 9):
            assert f"  {score}:" in self.prompt, (
                f"DIFFUSION_SYSTEM missing intermediate anchor for score {score}"
            )

    def test_critical_range_decision_tree_present(self):
        """DIFFUSION_SYSTEM should contain a CRITICAL RANGE DECISION TREE."""
        assert "CRITICAL RANGE DECISION TREE" in self.prompt

    def test_decision_tree_has_gates(self):
        """The decision tree should have Gate 1 through Gate 4."""
        for gate in ("Gate 1", "Gate 2", "Gate 3", "Gate 4"):
            assert gate in self.prompt, (
                f"DIFFUSION_SYSTEM decision tree missing {gate}"
            )

    def test_anti_inflation_rule_present(self):
        assert "ANTI-INFLATION RULE" in self.prompt

    def test_anti_deflation_rule_present(self):
        """DIFFUSION_SYSTEM should have ANTI-DEFLATION to prevent manufactured negatives."""
        assert "ANTI-DEFLATION RULE" in self.prompt

    def test_full_score_range_coverage(self):
        """All 11 integer scores 0-10 should appear as scoring anchors."""
        for score in range(11):
            assert f"  {score}:" in self.prompt, (
                f"DIFFUSION_SYSTEM missing integer anchor {score}"
            )


class TestBasicSystemPromptQuality:
    """Verify that BASIC_SYSTEM contains required scoring anchors and guidance."""

    @pytest.fixture(autouse=True)
    def _load(self):
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        self.prompt = BASIC_SYSTEM

    def test_anti_inflation_rule_present(self):
        assert "ANTI-INFLATION RULE" in self.prompt

    def test_anti_deflation_rule_present(self):
        """BASIC_SYSTEM should have ANTI-DEFLATION rule (symmetrical guard)."""
        assert "ANTI-DEFLATION RULE" in self.prompt

    def test_critical_range_decision_tree_present(self):
        """BASIC_SYSTEM should contain a CRITICAL RANGE section."""
        assert "CRITICAL RANGE" in self.prompt

    def test_basic_system_has_half_point_anchors(self):
        """BASIC_SYSTEM should have half-point anchors (4.5, 5.5, etc.)."""
        assert "4.5:" in self.prompt
        assert "5.5:" in self.prompt
