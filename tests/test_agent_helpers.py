"""Tests for harness/agent/agent_loop.py — pure helper functions.

Focuses on testable units that don't require a running LLM or network:
  - AgentConfig validation and defaults
  - AgentResult dataclass
  - AgentLoop._read_notes / _append_notes
  - AgentLoop._extract_cycle_summary
  - AgentLoop._persist_cycle
  - AgentLoop._build_system
  - AgentLoop._pause_file_path
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.agent.agent_loop import AgentConfig, AgentLoop, AgentResult
from harness.core.config import HarnessConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_harness_config(workspace: str = "/tmp") -> HarnessConfig:
    """Minimal HarnessConfig for testing."""
    return HarnessConfig(
        model="gpt-4o-mini",
        workspace=workspace,
        api_key="test-key",
    )


def _make_agent_config(workspace: str = "/tmp", **kwargs) -> AgentConfig:
    return AgentConfig(
        harness=_make_harness_config(workspace=workspace),
        **kwargs,
    )


def _make_loop(tmp_path: Path, **kwargs) -> AgentLoop:
    """Build an AgentLoop with mocked artifacts."""
    cfg = _make_agent_config(workspace=str(tmp_path), **kwargs)
    loop = AgentLoop.__new__(AgentLoop)  # skip __init__ async side-effects
    loop.config = cfg
    loop._notes_path = tmp_path / "agent_notes.md"
    loop.artifacts = MagicMock()
    loop.artifacts.write = MagicMock()
    return loop


# ===========================================================================
# AgentConfig
# ===========================================================================


class TestAgentConfig:
    def test_defaults_are_set(self, tmp_path: Path):
        cfg = _make_agent_config(workspace=str(tmp_path))
        assert cfg.max_cycles == 999
        assert cfg.continuous is False
        assert cfg.max_notes_cycles == 30
        assert cfg.auto_commit is True
        assert cfg.auto_push is False
        assert cfg.pause_file == ".harness.pause"

    def test_mission_stored(self, tmp_path: Path):
        cfg = _make_agent_config(workspace=str(tmp_path), mission="Fix all bugs.")
        assert cfg.mission == "Fix all bugs."

    def test_empty_mission_is_ok(self, tmp_path: Path):
        cfg = _make_agent_config(workspace=str(tmp_path), mission="")
        assert cfg.mission == ""

    def test_max_cycles_zero_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="max_cycles must be >= 1"):
            _make_agent_config(workspace=str(tmp_path), max_cycles=0)

    def test_max_cycles_negative_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="max_cycles must be >= 1"):
            _make_agent_config(workspace=str(tmp_path), max_cycles=-5)

    def test_max_notes_cycles_zero_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="max_notes_cycles must be >= 1"):
            _make_agent_config(workspace=str(tmp_path), max_notes_cycles=0)

    def test_mission_non_string_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="mission must be a string"):
            _make_agent_config(workspace=str(tmp_path), mission=42)  # type: ignore[arg-type]

    def test_cycle_hooks_defaults(self, tmp_path: Path):
        cfg = _make_agent_config(workspace=str(tmp_path))
        assert "syntax" in cfg.cycle_hooks
        assert "static" in cfg.cycle_hooks
        assert "import_smoke" in cfg.cycle_hooks

    def test_custom_cycle_hooks(self, tmp_path: Path):
        cfg = _make_agent_config(workspace=str(tmp_path), cycle_hooks=["syntax"])
        assert cfg.cycle_hooks == ["syntax"]


class TestAgentConfigFromDict:
    def test_missing_harness_raises(self):
        with pytest.raises(ValueError, match="agent config requires a 'harness' object"):
            AgentConfig.from_dict({"mission": "test"})

    def test_harness_not_dict_raises(self):
        with pytest.raises(ValueError, match="agent config requires a 'harness' object"):
            AgentConfig.from_dict({"harness": "not_a_dict"})

    def test_comment_keys_stripped(self, tmp_path: Path):
        data = {
            "harness": {"model": "gpt-4o-mini", "workspace": str(tmp_path), "api_key": "k"},
            "mission": "Build something",
            "// comment": "ignored",
            "_internal": "ignored",
        }
        cfg = AgentConfig.from_dict(data)
        assert cfg.mission == "Build something"
        assert not hasattr(cfg, "// comment")

    def test_valid_minimal_config(self, tmp_path: Path):
        data = {
            "harness": {"model": "gpt-4o-mini", "workspace": str(tmp_path), "api_key": "k"},
        }
        cfg = AgentConfig.from_dict(data)
        assert cfg.mission == ""


# ===========================================================================
# AgentResult
# ===========================================================================


class TestAgentResult:
    def test_basic_construction(self):
        r = AgentResult(
            success=True,
            cycles_run=5,
            mission_status="complete",
            total_tool_calls=42,
            summary="All done.",
        )
        assert r.success is True
        assert r.cycles_run == 5
        assert r.mission_status == "complete"
        assert r.total_tool_calls == 42
        assert r.summary == "All done."
        assert r.run_dir == ""

    def test_run_dir_set(self):
        r = AgentResult(
            success=False,
            cycles_run=1,
            mission_status="partial",
            total_tool_calls=0,
            summary="",
            run_dir="harness_output/run_123",
        )
        assert r.run_dir == "harness_output/run_123"


# ===========================================================================
# AgentLoop._read_notes
# ===========================================================================


class TestAgentLoopReadNotes:
    def test_returns_empty_when_file_missing(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        assert loop._read_notes() == ""

    def test_returns_content_when_file_exists(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._notes_path.write_text("## Cycle 1 Summary (2024-01-01)\nDid stuff.", encoding="utf-8")
        notes = loop._read_notes()
        assert "Did stuff." in notes

    def test_keeps_last_n_cycles(self, tmp_path: Path):
        loop = _make_loop(tmp_path, max_notes_cycles=2)
        content = (
            "## Cycle 1 Summary (2024-01-01)\nCycle one work.\n"
            "## Cycle 2 Summary (2024-01-02)\nCycle two work.\n"
            "## Cycle 3 Summary (2024-01-03)\nCycle three work.\n"
        )
        loop._notes_path.write_text(content, encoding="utf-8")
        notes = loop._read_notes()
        # Should have cycles 2 and 3 but not 1
        assert "Cycle three work." in notes
        assert "Cycle two work." in notes
        assert "Cycle one work." not in notes

    def test_max_notes_cycles_one(self, tmp_path: Path):
        loop = _make_loop(tmp_path, max_notes_cycles=1)
        content = (
            "## Cycle 1 Summary (2024)\nOld stuff.\n"
            "## Cycle 2 Summary (2024)\nNew stuff.\n"
        )
        loop._notes_path.write_text(content, encoding="utf-8")
        notes = loop._read_notes()
        assert "New stuff." in notes
        assert "Old stuff." not in notes

    def test_returns_empty_on_oserror(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        # Simulate file existing but unreadable by monkeypatching Path.read_text
        loop._notes_path.write_text("content", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("perm denied")):
            result = loop._read_notes()
        assert result == ""

    def test_single_cycle_block_returned_in_full(self, tmp_path: Path):
        loop = _make_loop(tmp_path, max_notes_cycles=30)
        content = "## Cycle 5 Summary (2024)\nImportant info."
        loop._notes_path.write_text(content, encoding="utf-8")
        notes = loop._read_notes()
        assert "Important info." in notes
        assert "Cycle 5" in notes


# ===========================================================================
# AgentLoop._append_notes
# ===========================================================================


class TestAgentLoopAppendNotes:
    def test_creates_file_if_missing(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._append_notes(cycle=0, summary="First cycle summary.")
        assert loop._notes_path.exists()
        content = loop._notes_path.read_text(encoding="utf-8")
        assert "First cycle summary." in content

    def test_appends_cycle_number(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._append_notes(cycle=2, summary="Third cycle.")
        content = loop._notes_path.read_text(encoding="utf-8")
        assert "## Cycle 3 Summary" in content

    def test_appends_to_existing_file(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._notes_path.write_text("## Cycle 1 Summary (t)\nOld.\n", encoding="utf-8")
        loop._append_notes(cycle=1, summary="Second cycle.")
        content = loop._notes_path.read_text(encoding="utf-8")
        assert "Old." in content
        assert "Second cycle." in content

    def test_multiple_appends_accumulate(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._append_notes(cycle=0, summary="Cycle 1")
        loop._append_notes(cycle=1, summary="Cycle 2")
        loop._append_notes(cycle=2, summary="Cycle 3")
        content = loop._notes_path.read_text(encoding="utf-8")
        assert "Cycle 1" in content
        assert "Cycle 2" in content
        assert "Cycle 3" in content

    def test_oserror_logged_not_raised(self, tmp_path: Path, caplog):
        import logging
        loop = _make_loop(tmp_path)
        # Point notes path to a non-writable location by using a file as parent dir
        fake_dir = tmp_path / "not_a_dir"
        fake_dir.write_text("I am a file", encoding="utf-8")
        loop._notes_path = fake_dir / "notes.md"  # parent is a file, not a dir
        with caplog.at_level(logging.WARNING):
            loop._append_notes(cycle=0, summary="test")  # must not raise
        # Warning should have been logged
        assert any("failed to append notes" in r.message for r in caplog.records)

    def test_timestamp_in_header(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._append_notes(cycle=0, summary="summary text")
        content = loop._notes_path.read_text(encoding="utf-8")
        # ISO timestamp pattern: YYYY-MM-DDTHH:MM:SS
        assert re.search(r"\d{4}-\d{2}-\d{2}T", content)


# ===========================================================================
# AgentLoop._extract_cycle_summary
# ===========================================================================


class TestExtractCycleSummary:
    def test_no_text_no_tools(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        result = loop._extract_cycle_summary(
            text="", exec_log=[], hook_failures=[]
        )
        assert "Tool usage: (none)" in result
        assert "total=0" in result

    def test_text_tail_included(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        result = loop._extract_cycle_summary(
            text="Short output.", exec_log=[], hook_failures=[]
        )
        assert "Short output." in result

    def test_long_text_truncated(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        long_text = "x" * 2000
        result = loop._extract_cycle_summary(
            text=long_text, exec_log=[], hook_failures=[]
        )
        # Only last 500 chars kept
        assert "\u2026" in result  # ellipsis at the start
        # The tail should be present
        assert "x" * 100 in result

    def test_tool_counts_summarised(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        exec_log = [
            {"tool": "batch_read"},
            {"tool": "batch_read"},
            {"tool": "bash"},
            {"tool": "bash"},
            {"tool": "bash"},
        ]
        result = loop._extract_cycle_summary(
            text="", exec_log=exec_log, hook_failures=[]
        )
        assert "bash\xd73" in result
        assert "batch_read\xd72" in result
        assert "total=5" in result

    def test_hook_failures_included(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        result = loop._extract_cycle_summary(
            text="", exec_log=[], hook_failures=["static_check: F401 unused import"]
        )
        assert "HOOK FAILURES" in result
        assert "F401" in result

    def test_multiple_hook_failures_joined(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        result = loop._extract_cycle_summary(
            text="", exec_log=[], hook_failures=["err1", "err2"]
        )
        assert "err1" in result
        assert "err2" in result

    def test_no_hook_failures_not_in_output(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        result = loop._extract_cycle_summary(
            text="done", exec_log=[], hook_failures=[]
        )
        assert "HOOK FAILURES" not in result

    def test_top_10_tools_only(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        # 15 distinct tool types
        exec_log = [{"tool": f"tool_{i}"} for i in range(15)]
        result = loop._extract_cycle_summary(
            text="", exec_log=exec_log, hook_failures=[]
        )
        # There should be at most 10 tools listed
        tool_line = [line for line in result.splitlines() if "Tool usage:" in line][0]
        # Count the number of tool entries (separated by commas)
        tools = [t.strip() for t in tool_line.split("Tool usage:")[1].split(",") if t.strip()]
        assert len(tools) <= 10


# ===========================================================================
# AgentLoop._persist_cycle
# ===========================================================================


class TestPersistCycle:
    def test_writes_text_to_output_txt(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._persist_cycle(cycle=0, text="my output", exec_log=[], hook_failures=[])
        loop.artifacts.write.assert_any_call("my output", "cycle_1", "output.txt")

    def test_writes_tool_log_json(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        exec_log = [{"tool": "bash", "success": True}]
        loop._persist_cycle(cycle=0, text="", exec_log=exec_log, hook_failures=[])
        # Second call should be for the JSON log
        calls = loop.artifacts.write.call_args_list
        json_calls = [c for c in calls if "tool_log.json" in c.args]
        assert len(json_calls) == 1
        json_data = json.loads(json_calls[0].args[0])
        assert json_data[0]["tool"] == "bash"

    def test_writes_hook_failures_txt(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._persist_cycle(
            cycle=1, text="", exec_log=[], hook_failures=["static: E501"]
        )
        calls = loop.artifacts.write.call_args_list
        hook_calls = [c for c in calls if "hook_failures.txt" in c.args]
        assert len(hook_calls) == 1
        assert "static: E501" in hook_calls[0].args[0]

    def test_no_hook_failures_file_when_empty(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._persist_cycle(cycle=0, text="", exec_log=[], hook_failures=[])
        calls = loop.artifacts.write.call_args_list
        hook_calls = [c for c in calls if "hook_failures.txt" in str(c)]
        assert len(hook_calls) == 0

    def test_cycle_index_in_segment_name(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._persist_cycle(cycle=4, text="", exec_log=[], hook_failures=[])
        calls = loop.artifacts.write.call_args_list
        # All calls should use "cycle_5" as segment name
        segments = {c.args[1] for c in calls}
        assert "cycle_5" in segments

    def test_exception_during_write_not_propagated(self, tmp_path: Path, caplog):
        import logging
        loop = _make_loop(tmp_path)
        loop.artifacts.write.side_effect = OSError("disk full")
        with caplog.at_level(logging.WARNING):
            loop._persist_cycle(cycle=0, text="x", exec_log=[], hook_failures=[])  # must not raise
        assert any("failed to persist" in r.message for r in caplog.records)


# ===========================================================================
# AgentLoop._build_system
# ===========================================================================


class TestBuildSystem:
    def test_includes_mission(self, tmp_path: Path):
        loop = _make_loop(tmp_path, mission="Fix the critical bug.")
        system = loop._build_system(cycle=0)
        assert "Fix the critical bug." in system

    def test_includes_cycle_number(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        system = loop._build_system(cycle=4)
        assert "Cycle 5" in system

    def test_includes_max_cycles(self, tmp_path: Path):
        loop = _make_loop(tmp_path, max_cycles=10)
        system = loop._build_system(cycle=0)
        assert "10" in system

    def test_no_mission_section_when_empty(self, tmp_path: Path):
        loop = _make_loop(tmp_path, mission="")
        system = loop._build_system(cycle=0)
        # Should still be valid
        assert "Cycle 1" in system

    def test_includes_previous_notes(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        loop._notes_path.write_text(
            "## Cycle 1 Summary (t)\nPrevious work done.", encoding="utf-8"
        )
        system = loop._build_system(cycle=1)
        assert "Previous work done." in system

    def test_no_notes_section_when_file_missing(self, tmp_path: Path):
        loop = _make_loop(tmp_path)
        system = loop._build_system(cycle=0)
        assert "Persistent Notes" not in system

    def test_continuous_mode_uses_different_rules(self, tmp_path: Path):
        loop_cont = _make_loop(tmp_path, continuous=True)
        loop_one = _make_loop(tmp_path, continuous=False)
        sys_cont = loop_cont._build_system(cycle=0)
        sys_one = loop_one._build_system(cycle=0)
        # They should differ (different completion rules)
        assert sys_cont != sys_one


# ===========================================================================
# AgentLoop._pause_file_path
# ===========================================================================


class TestPauseFilePath:
    def test_relative_to_workspace(self, tmp_path: Path):
        loop = _make_loop(tmp_path, pause_file=".harness.pause")
        p = loop._pause_file_path()
        assert p == tmp_path / ".harness.pause"

    def test_absolute_path_used_as_is(self, tmp_path: Path):
        loop = _make_loop(tmp_path, pause_file=str(tmp_path / "custom_pause"))
        p = loop._pause_file_path()
        assert p == tmp_path / "custom_pause"

    def test_custom_relative_pause_file(self, tmp_path: Path):
        loop = _make_loop(tmp_path, pause_file="subdir/pause")
        p = loop._pause_file_path()
        assert p == tmp_path / "subdir" / "pause"
