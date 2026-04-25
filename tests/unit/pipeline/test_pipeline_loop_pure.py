"""Unit tests for pure helper functions in harness.pipeline.pipeline_loop.

This covers functions that can be tested in isolation without spinning
up the async loop (no LLM calls, no file system writes).
"""

from harness.pipeline.pipeline_loop import _read_best_score_from_summary, _BEST_SCORE_RE


# ---------------------------------------------------------------------------
# _read_best_score_from_summary
# ---------------------------------------------------------------------------

class TestReadBestScoreFromSummary:
    """Parse the best score from a PhaseRunner phase_summary.txt string."""

    # --- Success cases ---

    def test_integer_score(self) -> None:
        assert _read_best_score_from_summary("**Best**: 8") == 8.0

    def test_float_score_one_decimal(self) -> None:
        assert _read_best_score_from_summary("**Best**: 8.5") == 8.5

    def test_float_score_two_decimals(self) -> None:
        assert _read_best_score_from_summary("**Best**: 7.25") == 7.25

    def test_perfect_score_10(self) -> None:
        assert _read_best_score_from_summary("**Best**: 10") == 10.0

    def test_minimum_score_0(self) -> None:
        assert _read_best_score_from_summary("**Best**: 0") == 0.0

    def test_score_in_multiline_summary(self) -> None:
        text = (
            "## Phase Summary\n"
            "\n"
            "Round 1: 6.5\n"
            "Round 2: 7.8\n"
            "**Best**: 7.8\n"
            "Criterion: tests pass"
        )
        assert _read_best_score_from_summary(text) == 7.8

    def test_returns_first_match_when_two_present(self) -> None:
        # regex searches and returns the first match
        text = "**Best**: 8.5\n**Best**: 9.0"
        result = _read_best_score_from_summary(text)
        assert result == 8.5

    def test_score_at_end_of_file_no_trailing_newline(self) -> None:
        assert _read_best_score_from_summary("**Best**: 9.3") == 9.3

    def test_score_with_leading_whitespace_on_line(self) -> None:
        # Pattern requires ^, so leading whitespace on that line should NOT match
        text = "   **Best**: 8.0"
        assert _read_best_score_from_summary(text) is None

    def test_score_after_blank_line(self) -> None:
        text = "Some header\n\n**Best**: 6.0\n"
        assert _read_best_score_from_summary(text) == 6.0

    def test_returns_float_type(self) -> None:
        result = _read_best_score_from_summary("**Best**: 7")
        assert isinstance(result, float)

    # --- Failure cases: returns None ---

    def test_empty_string_returns_none(self) -> None:
        assert _read_best_score_from_summary("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _read_best_score_from_summary("   \n  \n  ") is None

    def test_no_pattern_returns_none(self) -> None:
        assert _read_best_score_from_summary("Round 1: 8.5\nNo best line here") is None

    def test_wrong_markdown_format_returns_none(self) -> None:
        # Missing double asterisks
        assert _read_best_score_from_summary("Best: 8.5") is None

    def test_partial_bold_returns_none(self) -> None:
        assert _read_best_score_from_summary("*Best*: 8.5") is None

    def test_text_after_keyword_returns_none(self) -> None:
        # 'Best round' is not the same as 'Best'
        assert _read_best_score_from_summary("**Best round**: 8.5") is None

    def test_score_in_middle_of_line_returns_none(self) -> None:
        # regex uses ^, so indented ** patterns don't match
        text = "  **Best**: 7.5"  # indented
        assert _read_best_score_from_summary(text) is None

    def test_non_numeric_value_returns_none(self) -> None:
        # 'N/A' is not a number
        assert _read_best_score_from_summary("**Best**: N/A") is None

    def test_score_missing_entirely(self) -> None:
        text = "**Worst**: 2.0\n**Median**: 5.0"
        assert _read_best_score_from_summary(text) is None


# ---------------------------------------------------------------------------
# _BEST_SCORE_RE regex
# ---------------------------------------------------------------------------

class TestBestScoreRegex:
    """Direct tests of the compiled regex pattern."""

    def test_regex_matches_integer(self) -> None:
        m = _BEST_SCORE_RE.search("**Best**: 9")
        assert m is not None
        assert m.group(1) == "9"

    def test_regex_matches_float(self) -> None:
        m = _BEST_SCORE_RE.search("**Best**: 8.5")
        assert m is not None
        assert m.group(1) == "8.5"

    def test_regex_multiline_finds_line(self) -> None:
        text = "Line 1\n**Best**: 7.25\nLine 3"
        m = _BEST_SCORE_RE.search(text)
        assert m is not None
        assert m.group(1) == "7.25"

    def test_regex_does_not_match_indented(self) -> None:
        m = _BEST_SCORE_RE.search("  **Best**: 8.0")
        assert m is None

    def test_regex_does_not_match_partial_bold(self) -> None:
        m = _BEST_SCORE_RE.search("*Best*: 8.0")
        assert m is None

    def test_regex_does_not_match_wrong_keyword(self) -> None:
        m = _BEST_SCORE_RE.search("**Worst**: 8.0")
        assert m is None

    def test_regex_group1_captures_numeric_value(self) -> None:
        m = _BEST_SCORE_RE.search("**Best**: 10")
        assert m is not None
        val = float(m.group(1))
        assert val == 10.0
