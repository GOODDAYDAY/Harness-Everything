"""Tests for harness/tools/python_eval.py (PythonEvalTool)."""
from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

from harness.tools.python_eval import PythonEvalTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: pathlib.Path):
    cfg = MagicMock()
    cfg.workspace = str(tmp_path)
    cfg.allowed_paths = [tmp_path]
    return cfg


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tool():
    return PythonEvalTool()


# ---------------------------------------------------------------------------
# Basic evaluation
# ---------------------------------------------------------------------------

class TestBasicEvaluation:
    def test_expression_return_value(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="1 + 1"))
        assert not r.is_error
        assert "2" in r.output

    def test_multiline_snippet(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="x = 3\nx * 4"))
        assert not r.is_error
        assert "12" in r.output

    def test_print_shows_in_stdout(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet='print("hello world")'))
        assert not r.is_error
        assert "hello world" in r.output

    def test_return_value_section_present(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="42"))
        assert "[return value]" in r.output
        assert "42" in r.output

    def test_assignment_no_error(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="x = 5"))
        assert not r.is_error
        # Pure assignment still produces successful exit
        assert "[exit code: 0]" in r.output


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_division_by_zero_is_error(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="1 / 0"))
        assert r.is_error

    def test_syntax_error_is_error(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="def bad(:"))
        assert r.is_error

    def test_import_error_is_error(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="import nonexistent_module_xyz"))
        assert r.is_error

    def test_error_output_contains_traceback(self, tool, tmp_path):
        r = _run(tool.execute(_cfg(tmp_path), snippet="raise ValueError('test err')"))
        assert r.is_error
        assert "ValueError" in r.output


# ---------------------------------------------------------------------------
# stderr handling
# ---------------------------------------------------------------------------

class TestStderr:
    def test_stderr_appears_in_output(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet="import sys; print('err msg', file=sys.stderr)"
        ))
        # stderr output should appear without marking as error
        assert "err msg" in r.output

    def test_stderr_section_label(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet="import sys; print('warn', file=sys.stderr)"
        ))
        assert "[stderr]" in r.output


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_triggers_error(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet="import time; time.sleep(10)",
            timeout=1
        ))
        assert r.is_error

    def test_timeout_message_mentions_timeout(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet="import time; time.sleep(10)",
            timeout=1
        ))
        # Should mention 'timed out' or similar
        text = (r.output + r.error).lower()
        assert "timeout" in text or "timed" in text


# ---------------------------------------------------------------------------
# max_output_chars truncation
# ---------------------------------------------------------------------------

class TestOutputTruncation:
    def test_output_truncated_at_limit(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet='print("x" * 2000)',
            max_output_chars=100
        ))
        # Total output length should not far exceed the limit
        assert len(r.output) < 500  # some overhead for section headers

    def test_truncation_note_present(self, tool, tmp_path):
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet='print("y" * 2000)',
            max_output_chars=50
        ))
        assert "truncated" in r.output.lower()


# ---------------------------------------------------------------------------
# Import from workspace
# ---------------------------------------------------------------------------

class TestWorkspaceImport:
    def test_can_import_local_module(self, tool, tmp_path):
        (tmp_path / "mymod.py").write_text("VALUE = 99\n")
        r = _run(tool.execute(
            _cfg(tmp_path),
            snippet="import mymod; mymod.VALUE"
        ))
        assert not r.is_error
        assert "99" in r.output
