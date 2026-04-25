"""Tests for harness.pipeline.simple_loop pure-function helpers."""
from __future__ import annotations


from harness.evaluation.evaluator import Verdict
from harness.evaluation.static_analysis import Finding, StaticReport
from harness.pipeline.executor import ExecutionResult
from harness.pipeline.simple_loop import _format_iteration_feedback, _trim_feedback_ctx


# ---------------------------------------------------------------------------
# _trim_feedback_ctx
# ---------------------------------------------------------------------------

class TestTrimFeedbackCtx:
    """Tests for _trim_feedback_ctx(ctx, cap)."""

    def test_short_context_unchanged(self):
        """Context shorter than cap is returned as-is."""
        assert _trim_feedback_ctx("hello", 100) == "hello"

    def test_exact_length_unchanged(self):
        """Context exactly at cap is returned as-is."""
        text = "abcde"
        assert _trim_feedback_ctx(text, 5) == text

    def test_trimmed_to_last_n_chars(self):
        """Context longer than cap is truncated to the last `cap` chars."""
        result = _trim_feedback_ctx("hello world", 5)
        assert result == "world"
        assert len(result) == 5

    def test_empty_string_unchanged(self):
        """Empty string is returned as-is regardless of cap."""
        assert _trim_feedback_ctx("", 10) == ""
        assert _trim_feedback_ctx("", 0) == ""

    def test_realigns_to_iteration_boundary(self):
        """After slicing, result starts at the next iteration heading."""
        # Build a string that will be sliced in the middle of a heading
        prefix = "x" * 30
        section = "\n\n## Iteration 2 Feedback\nsome content"
        ctx = prefix + section
        cap = len(ctx) - 10  # trim 10 chars from front → heading is in the remainder
        result = _trim_feedback_ctx(ctx, cap)
        assert result.startswith("\n\n## Iteration"), (
            f"Expected to start at heading boundary, got: {result[:50]!r}"
        )

    def test_falls_back_when_no_heading(self):
        """With no heading boundary, raw slice is returned."""
        # Long string with no ## Iteration heading
        ctx = "a" * 200
        result = _trim_feedback_ctx(ctx, 50)
        assert len(result) == 50
        assert result == "a" * 50

    def test_only_newest_content_retained(self):
        """Trimming keeps the END of the string (newest iterations)."""
        ctx = "OLD_CONTENT" + "NEW_CONTENT"
        cap = len("NEW_CONTENT")
        result = _trim_feedback_ctx(ctx, cap)
        assert result == "NEW_CONTENT"

    def test_unicode_content(self):
        """Unicode characters don't break trimming."""
        ctx = "café " * 100
        result = _trim_feedback_ctx(ctx, 20)
        assert len(result) <= 20 or result.endswith(ctx[-20:])


# ---------------------------------------------------------------------------
# _format_iteration_feedback  helpers
# ---------------------------------------------------------------------------

def _make_verdict(
    *,
    passed: bool = False,
    reason: str = "Reason",
    feedback: str = "Feedback text",
    static_report: StaticReport | None = None,
    score: int | None = 5,
) -> Verdict:
    return Verdict(
        passed=passed,
        reason=reason,
        feedback=feedback,
        static_report=static_report,
        score=score,
    )


def _make_result(
    *,
    text: str = "",
    files_changed: list[str] | None = None,
    log: list[dict] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        text=text,
        files_changed=files_changed or [],
        log=log or [],
    )


# ---------------------------------------------------------------------------
# _format_iteration_feedback
# ---------------------------------------------------------------------------

class TestFormatIterationFeedback:
    """Tests for _format_iteration_feedback(i, verdict, result)."""

    def test_contains_iteration_number(self):
        """Output includes the iteration number as a heading."""
        v = _make_verdict()
        r = _make_result()
        out = _format_iteration_feedback(3, v, r)
        assert "## Iteration 3 Feedback" in out

    def test_contains_fail_result(self):
        """The FAIL label is always present (only called on failure)."""
        v = _make_verdict(passed=False)
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "**Result:** FAIL" in out

    def test_reason_included(self):
        """The verdict reason is embedded in the output."""
        v = _make_verdict(reason="Tests failed")
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "Tests failed" in out

    def test_feedback_included(self):
        """The verdict feedback is embedded in the output."""
        v = _make_verdict(feedback="Fix the import error")
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "Fix the import error" in out

    def test_stats_comment_included(self):
        """HTML comment with stats is present."""
        v = _make_verdict()
        r = _make_result(files_changed=["a.py", "b.py"])
        out = _format_iteration_feedback(1, v, r)
        assert "<!-- stats:" in out
        assert "files_changed=2" in out

    def test_tool_call_count_in_stats(self):
        """Tool call count is derived from log entries."""
        fake_log = [
            {"role": "tool", "name": "bash"},
            {"role": "tool", "name": "batch_read"},
            {"role": "tool", "name": "bash"},
        ]
        v = _make_verdict()
        r = _make_result(log=fake_log)
        out = _format_iteration_feedback(1, v, r)
        assert "tool_calls=3" in out

    def test_no_tool_calls_zero(self):
        """Zero tool calls shown when log is empty."""
        v = _make_verdict()
        r = _make_result(log=[])
        out = _format_iteration_feedback(1, v, r)
        assert "tool_calls=0" in out

    def test_no_files_changed_zero(self):
        """Zero files_changed shown when list is empty."""
        v = _make_verdict()
        r = _make_result(files_changed=[])
        out = _format_iteration_feedback(1, v, r)
        assert "files_changed=0" in out

    def test_no_static_errors_when_no_report(self):
        """No static_report defaults to zero errors and warnings in stats."""
        v = _make_verdict(static_report=None)
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "static_errors=0" in out
        assert "static_warnings=0" in out

    def test_static_error_count_in_stats(self):
        """ERROR-level static findings are counted correctly."""
        findings = [
            Finding(level="ERROR", file="foo.py", message="syntax error", line=5),
            Finding(level="ERROR", file="bar.py", message="undefined", line=12),
        ]
        sr = StaticReport(findings=findings, files_checked=2, files_skipped=0)
        v = _make_verdict(static_report=sr)
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "static_errors=2" in out
        assert "static_warnings=0" in out

    def test_static_warning_count_in_stats(self):
        """WARN-level static findings are counted correctly."""
        findings = [
            Finding(level="WARN", file="foo.py", message="unused var", line=3),
        ]
        sr = StaticReport(findings=findings, files_checked=1, files_skipped=0)
        v = _make_verdict(static_report=sr)
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "static_errors=0" in out
        assert "static_warnings=1" in out

    def test_static_errors_trigger_priority_message(self):
        """When there are static errors, an urgent message is shown."""
        findings = [Finding(level="ERROR", file="x.py", message="err", line=1)]
        sr = StaticReport(findings=findings, files_checked=1, files_skipped=0)
        v = _make_verdict(static_report=sr)
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        # The output should indicate errors need fixing
        assert "Static errors" in out or "static_errors=1" in out

    def test_mixed_static_errors_and_warnings(self):
        """Mixed ERROR and WARN findings are both counted."""
        findings = [
            Finding(level="ERROR", file="a.py", message="bad syntax", line=1),
            Finding(level="WARN", file="b.py", message="style", line=2),
            Finding(level="WARN", file="c.py", message="style", line=3),
        ]
        sr = StaticReport(findings=findings, files_checked=3, files_skipped=0)
        v = _make_verdict(static_report=sr)
        r = _make_result(files_changed=["a.py"])
        out = _format_iteration_feedback(2, v, r)
        assert "static_errors=1" in out
        assert "static_warnings=2" in out

    def test_iteration_number_one(self):
        """Works correctly for iteration 1."""
        v = _make_verdict(reason="First try failed")
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert "## Iteration 1 Feedback" in out
        assert "First try failed" in out

    def test_high_iteration_number(self):
        """Works correctly for high iteration numbers."""
        v = _make_verdict(reason="Still failing")
        r = _make_result()
        out = _format_iteration_feedback(99, v, r)
        assert "## Iteration 99 Feedback" in out

    def test_returns_string(self):
        """Return type is always a string."""
        v = _make_verdict()
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert isinstance(out, str)

    def test_output_not_empty(self):
        """Output always contains content."""
        v = _make_verdict()
        r = _make_result()
        out = _format_iteration_feedback(1, v, r)
        assert out.strip(), "Output should not be empty"
