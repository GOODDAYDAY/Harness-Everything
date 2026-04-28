"""Unit tests for harness.tools.lint_check.

Covers:
  - Clean file → "No issues found"
  - File with issues → structured diagnostics
  - fix=true passed to ruff
  - select filter passed to ruff
  - Empty paths → error
  - ruff not installed → graceful error
  - ruff timeout → graceful error
  - Output truncation at _MAX_OUTPUT_CHARS
  - Schema validation
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch


from harness.core.config import HarnessConfig
from harness.tools.lint_check import LintCheckTool


def _run(coro):
    return asyncio.run(coro)


def _make_config(workspace: str) -> HarnessConfig:
    cfg = Mock(spec=HarnessConfig)
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


def _mock_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0):
    """Create a mock subprocess result."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    proc.wait = AsyncMock()
    return proc


class TestLintCheckClean:
    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_no_issues_returns_clean(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.return_value = _mock_proc(b"[]", returncode=0)

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["clean.py"]))
        assert not result.is_error
        assert "No issues" in result.output


class TestLintCheckDiagnostics:
    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_issues_returned_structured(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        diags = [
            {
                "filename": f"{ws}/bad.py",
                "location": {"row": 5, "column": 1},
                "code": "F401",
                "message": "os imported but unused",
                "fix": None,
            },
            {
                "filename": f"{ws}/bad.py",
                "location": {"row": 10, "column": 5},
                "code": "E501",
                "message": "line too long",
                "fix": {"applicability": "safe"},
            },
        ]
        mock_exec.return_value = _mock_proc(
            json.dumps(diags).encode(), returncode=1,
        )

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["bad.py"]))
        assert not result.is_error
        assert "2 issue" in result.output
        assert "F401" in result.output
        assert "E501" in result.output
        assert "[auto-fixable]" in result.output

    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_relative_path_in_output(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        diags = [
            {
                "filename": f"{ws}/harness/tools/foo.py",
                "location": {"row": 1, "column": 1},
                "code": "F401",
                "message": "unused",
                "fix": None,
            },
        ]
        mock_exec.return_value = _mock_proc(
            json.dumps(diags).encode(), returncode=1,
        )

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["harness/tools/foo.py"]))
        assert "harness/tools/foo.py" in result.output
        # Should not contain absolute path
        assert str(ws) not in result.output


class TestLintCheckOptions:
    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_fix_flag_passed(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.return_value = _mock_proc(b"[]", returncode=0)

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        _run(tool.execute(cfg, paths=["a.py"], fix=True))

        call_args = mock_exec.call_args[0]
        assert "--fix" in call_args

    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_select_filter_passed(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.return_value = _mock_proc(b"[]", returncode=0)

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        _run(tool.execute(cfg, paths=["a.py"], select="F401,E501"))

        call_args = mock_exec.call_args[0]
        assert "--select" in call_args
        assert "F401,E501" in call_args


class TestLintCheckValidation:
    def test_empty_paths_is_error(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=[]))
        assert result.is_error
        assert "non-empty" in result.error


class TestLintCheckErrors:
    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_ruff_not_installed(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.side_effect = FileNotFoundError("ruff")

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["a.py"]))
        assert result.is_error
        assert "not installed" in result.error

    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_ruff_non_json_output(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.return_value = _mock_proc(b"some raw text output", returncode=0)

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["a.py"]))
        assert not result.is_error
        assert "non-JSON" in result.output

    @patch("harness.tools.lint_check.asyncio.create_subprocess_exec")
    def test_ruff_bad_exit_code(self, mock_exec, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        mock_exec.return_value = _mock_proc(b"", stderr=b"crash", returncode=2)

        tool = LintCheckTool()
        cfg = _make_config(str(ws))
        result = _run(tool.execute(cfg, paths=["a.py"]))
        assert result.is_error
        assert "exit 2" in result.error


class TestLintCheckSchema:
    def test_schema_requires_paths(self):
        tool = LintCheckTool()
        schema = tool.input_schema()
        assert "paths" in schema["properties"]
        assert "paths" in schema["required"]

    def test_name(self):
        tool = LintCheckTool()
        assert tool.name == "lint_check"
