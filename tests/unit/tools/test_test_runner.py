"""Unit tests for harness/tools/test_runner.py.

Tests the pure helper functions (_parse_pytest_stdout, _format_results)
and the tool's integration with actual pytest execution.
"""
from __future__ import annotations

import json
import os
import textwrap
from unittest.mock import Mock

import pytest

from harness.core.config import HarnessConfig
from harness.tools.test_runner import (
    TestRunnerTool,
    _format_results,
    _parse_pytest_stdout,
)


# ---------------------------------------------------------------------------
# _parse_pytest_stdout — pure function tests
# ---------------------------------------------------------------------------

class TestParsePytestStdout:
    """Tests for the stdout parser — outcomes are UPPERCASE."""

    def test_all_passing(self):
        stdout = textwrap.dedent("""\
            tests/test_foo.py::test_one PASSED
            tests/test_foo.py::test_two PASSED
            ====== 2 passed in 0.12s ======
        """)
        result = _parse_pytest_stdout(stdout)
        assert result["passed"] == 2
        assert result["failed"] == 0
        assert result["error"] == 0
        assert result["skipped"] == 0
        assert result["total"] == 2
        assert abs(result["duration"] - 0.12) < 1e-6
        assert len(result["tests"]) == 2
        # Outcomes are stored uppercase from the regex match
        assert all(t["outcome"] == "PASSED" for t in result["tests"])

    def test_mixed_outcomes(self):
        stdout = textwrap.dedent("""\
            tests/test_foo.py::test_pass PASSED
            tests/test_foo.py::test_fail FAILED
            tests/test_foo.py::test_skip SKIPPED
            tests/test_foo.py::test_err  ERROR
            ====== 1 passed, 1 failed, 1 error, 1 skipped in 0.50s ======
        """)
        result = _parse_pytest_stdout(stdout)
        assert result["passed"] == 1
        assert result["failed"] == 1
        assert result["error"] == 1
        assert result["skipped"] == 1
        assert result["total"] == 4
        assert abs(result["duration"] - 0.50) < 1e-6

    def test_failure_tracebacks_captured(self):
        stdout = textwrap.dedent("""\
            tests/test_foo.py::test_fail FAILED
            ========================= FAILURES =========================
            _________________________ test_fail _________________________
            
            def test_fail():
            >   assert 1 == 2
            E   AssertionError: assert 1 == 2
            ====== 1 failed in 0.05s ======
        """)
        result = _parse_pytest_stdout(stdout)
        assert result["failed"] == 1
        assert len(result["failures"]) == 1
        fail = result["failures"][0]
        assert "test_fail" in fail["name"]
        # Traceback content should mention AssertionError
        assert "AssertionError" in fail["short_tb"]

    def test_empty_stdout(self):
        result = _parse_pytest_stdout("")
        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["total"] == 0
        assert result["tests"] == []
        assert result["failures"] == []

    def test_summary_with_duration_only(self):
        # Only a summary line with duration
        stdout = "====== 5 passed in 1.23s ======\n"
        result = _parse_pytest_stdout(stdout)
        assert result["passed"] == 5
        assert result["total"] == 5
        assert abs(result["duration"] - 1.23) < 1e-6

    def test_xfail_xpass_outcomes(self):
        stdout = textwrap.dedent("""\
            tests/test_foo.py::test_xfail XFAIL
            tests/test_foo.py::test_xpass XPASS
            ====== 2 passed in 0.10s ======
        """)
        result = _parse_pytest_stdout(stdout)
        outcomes = {t["outcome"] for t in result["tests"]}
        assert "XFAIL" in outcomes
        assert "XPASS" in outcomes

    def test_multiple_failures_captured(self):
        stdout = textwrap.dedent("""\
            tests/test_a.py::test_one FAILED
            tests/test_b.py::test_two FAILED
            _________________________ test_one _________________________
            
            def test_one():
            >   assert False
            E   AssertionError
            _________________________ test_two _________________________
            
            def test_two():
            >   raise ValueError("oops")
            E   ValueError: oops
            ====== 2 failed in 0.08s ======
        """)
        result = _parse_pytest_stdout(stdout)
        assert result["failed"] == 2
        assert len(result["failures"]) == 2
        names = [f["name"] for f in result["failures"]]
        assert any("test_one" in n for n in names)
        assert any("test_two" in n for n in names)

    def test_nodeid_preserved(self):
        stdout = textwrap.dedent("""\
            tests/unit/core/test_config.py::TestConfig::test_defaults PASSED
            ====== 1 passed in 0.01s ======
        """)
        result = _parse_pytest_stdout(stdout)
        assert len(result["tests"]) == 1
        assert result["tests"][0]["nodeid"] == "tests/unit/core/test_config.py::TestConfig::test_defaults"

    def test_duration_zero_when_missing(self):
        # When the summary line has no 'in Xs', parser can't extract counts either
        stdout = "====== no tests ran ======\n"
        result = _parse_pytest_stdout(stdout)
        assert result["duration"] == 0.0
        assert result["passed"] == 0


# ---------------------------------------------------------------------------
# _format_results — pure function tests
# ---------------------------------------------------------------------------

class TestFormatResults:
    """Tests for the text formatter."""

    def _make_results(self, *, passed=0, failed=0, error=0, skipped=0, tests=None, failures=None, duration=1.23):
        total = passed + failed + error + skipped
        return {
            "passed": passed,
            "failed": failed,
            "error": error,
            "skipped": skipped,
            "total": total,
            "duration": duration,
            "tests": tests or [],
            "failures": failures or [],
        }

    def test_all_pass_header(self):
        results = self._make_results(passed=3)
        text = _format_results(results, "tests/", exit_code=0, max_failures=10)
        assert "3 passed" in text
        assert "[PASS]" in text
        assert "[FAIL]" not in text

    def test_fail_header(self):
        results = self._make_results(passed=2, failed=1)
        text = _format_results(results, "tests/", exit_code=1, max_failures=10)
        assert "[FAIL]" in text
        assert "1 failed" in text

    def test_includes_test_list_with_symbols(self):
        tests = [
            {"nodeid": "tests/test_foo.py::test_one", "outcome": "PASSED"},
            {"nodeid": "tests/test_foo.py::test_two", "outcome": "FAILED"},
        ]
        results = self._make_results(passed=1, failed=1, tests=tests)
        text = _format_results(results, "tests/", exit_code=1, max_failures=10)
        assert "test_one" in text
        assert "test_two" in text
        # Passed = ✓, Failed = ✗
        assert "\u2713" in text  # ✓
        assert "\u2717" in text  # ✗

    def test_failure_traceback_included(self):
        failures = [{"name": "tests/test_foo.py::test_bad", "short_tb": "AssertionError: assert 1 == 2"}]
        results = self._make_results(failed=1, failures=failures)
        text = _format_results(results, "tests/", exit_code=1, max_failures=10)
        assert "AssertionError" in text
        assert "test_bad" in text

    def test_max_failures_limit(self):
        failures = [{"name": f"test_{i}", "short_tb": "err"} for i in range(5)]
        results = self._make_results(failed=5, failures=failures)
        text = _format_results(results, "tests/", exit_code=1, max_failures=2)
        # Only 2 shown, 3 remaining
        assert "3 more failure" in text

    def test_zero_tests(self):
        results = self._make_results()
        text = _format_results(results, "tests/", exit_code=0, max_failures=10)
        assert "0 passed" in text

    def test_duration_shown(self):
        results = self._make_results(passed=1, duration=1.23)
        text = _format_results(results, "tests/", exit_code=0, max_failures=10)
        assert "1.23s" in text

    def test_skipped_in_summary(self):
        results = self._make_results(passed=2, skipped=1)
        text = _format_results(results, "tests/", exit_code=0, max_failures=10)
        assert "1 skipped" in text

    def test_unknown_outcome_gets_question_mark(self):
        tests = [{"nodeid": "tests/test_foo.py::test_x", "outcome": "WEIRD"}]
        results = self._make_results(passed=0, tests=tests)
        text = _format_results(results, "tests/", exit_code=0, max_failures=10)
        assert "?" in text


# ---------------------------------------------------------------------------
# Integration: TestRunnerTool.execute()
# ---------------------------------------------------------------------------

class TestTestRunnerToolIntegration:
    """Integration tests that invoke pytest via TestRunnerTool.execute()."""

    @pytest.fixture(autouse=True)
    def _disable_json_report(self, monkeypatch):
        """Disable json-report to avoid dependency on the optional plugin."""
        monkeypatch.setattr(TestRunnerTool, "_json_report_available", False)

    @pytest.fixture
    def config(self):
        cfg = Mock(spec=HarnessConfig)
        cfg.workspace = os.getcwd()
        cfg.allowed_paths = [os.getcwd()]
        return cfg

    @pytest.fixture
    def tool(self):
        return TestRunnerTool()

    @pytest.mark.asyncio
    async def test_run_passing_tests_returns_pass(self, tool, config):
        """Running passing tests returns a PASS result."""
        result = await tool.execute(
            config,
            test_path="tests/unit/pipeline/test_phase_config.py",
            timeout=60,
            max_failures=5,
            pytest_args=[],
            format="text",
        )
        assert not result.is_error
        assert "PASS" in result.output
        assert "passed" in result.output

    @pytest.mark.asyncio
    async def test_run_with_json_format(self, tool, config):
        """JSON format returns parseable JSON with expected keys."""
        result = await tool.execute(
            config,
            test_path="tests/unit/pipeline/test_phase_config.py",
            timeout=60,
            max_failures=5,
            pytest_args=[],
            format="json",
        )
        assert not result.is_error
        data = json.loads(result.output)
        for key in ("passed", "failed", "error", "skipped", "total", "duration", "exit_code", "tests", "failures"):
            assert key in data, f"Missing key: {key}"
        assert data["passed"] > 0
        assert data["failed"] == 0
        assert data["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_run_nonexistent_path_returns_error(self, tool, config):
        """A test path that doesn't exist should return an error ToolResult."""
        result = await tool.execute(
            config,
            test_path="tests/does_not_exist_xyz/",
            timeout=30,
            max_failures=5,
            pytest_args=[],
            format="text",
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_run_with_filter_flag(self, tool, config):
        """Passing -k filter restricts test selection and still succeeds."""
        result = await tool.execute(
            config,
            test_path="tests/unit/pipeline/test_phase_config.py",
            timeout=30,
            max_failures=5,
            pytest_args=["-k", "test_phase"],
            format="text",
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_json_tests_list_populated(self, tool, config):
        """JSON output: tests list contains per-test entries with nodeid and outcome."""
        result = await tool.execute(
            config,
            test_path="tests/unit/pipeline/test_phase_config.py",
            timeout=60,
            max_failures=5,
            pytest_args=[],
            format="json",
        )
        assert not result.is_error
        data = json.loads(result.output)
        assert isinstance(data["tests"], list)
        assert len(data["tests"]) > 0
        for entry in data["tests"]:
            assert "nodeid" in entry
            assert "outcome" in entry
