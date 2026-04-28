"""Tests for harness/agent/cycle_metrics.py."""

from __future__ import annotations

import json

import pytest

from harness.agent.cycle_metrics import (
    CycleMetrics,
    collect_cycle_metrics,
    format_detailed_report,
    format_summary,
    metrics_to_dict,
    persist_cycle_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(
    tool: str,
    *,
    inp: dict | None = None,
    output: str = "ok",
    is_error: bool = False,
    duration_ms: int = 100,
) -> dict:
    """Build a minimal exec_log entry."""
    return {
        "tool": tool,
        "input": inp or {},
        "output": output,
        "duration_ms": duration_ms,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# collect_cycle_metrics — tool efficiency
# ---------------------------------------------------------------------------

class TestToolEfficiency:
    def test_empty_log(self):
        m = collect_cycle_metrics(1, [], [], [], 0.0)
        assert m.total_tool_calls == 0
        assert m.first_try_success_rate == 0.0
        assert m.bash_fraction == 0.0

    def test_counts_and_success_rate(self):
        log = [
            _entry("batch_read"),
            _entry("batch_edit"),
            _entry("bash"),
            _entry("grep_search", is_error=True),
        ]
        m = collect_cycle_metrics(1, log, [], [], 10.0)
        assert m.total_tool_calls == 4
        assert m.error_tool_calls == 1
        assert m.first_try_success_rate == 0.75
        assert m.read_calls == 2  # batch_read + grep_search
        assert m.write_calls == 1  # batch_edit
        assert m.bash_calls == 1
        assert m.bash_fraction == 0.25

    def test_read_write_ratio(self):
        log = [_entry("batch_read")] * 6 + [_entry("batch_edit")] * 2
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.read_write_ratio == 3.0

    def test_read_write_ratio_no_writes(self):
        log = [_entry("batch_read")]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.read_write_ratio == 0.0  # no division by zero

    def test_unique_tools(self):
        log = [
            _entry("batch_read"),
            _entry("batch_read"),
            _entry("grep_search"),
            _entry("bash"),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.unique_tools_used == 3

    def test_tool_distribution_sorted(self):
        log = [_entry("bash")] * 5 + [_entry("batch_read")] * 3 + [_entry("grep_search")]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        keys = list(m.tool_distribution.keys())
        assert keys[0] == "bash"
        assert keys[1] == "batch_read"

    def test_avg_duration(self):
        log = [
            _entry("batch_read", duration_ms=100),
            _entry("batch_read", duration_ms=300),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.avg_tool_duration_ms == 200.0

    def test_behaviour_signals(self):
        log = [
            _entry("scratchpad"),
            _entry("test_runner"),
            _entry("test_runner"),
            _entry("lint_check"),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.scratchpad_calls == 1
        assert m.test_runner_calls == 2
        assert m.lint_calls == 1


# ---------------------------------------------------------------------------
# collect_cycle_metrics — change efficiency
# ---------------------------------------------------------------------------

class TestChangeEfficiency:
    def test_turns_per_change(self):
        log = [_entry("batch_read")] * 8 + [_entry("batch_edit")] * 2
        m = collect_cycle_metrics(1, log, ["a.py", "b.py"], [], 0.0)
        assert m.files_changed == 2
        assert m.turns_per_change == 5.0

    def test_no_changes(self):
        log = [_entry("batch_read")]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.files_changed == 0
        assert m.turns_per_change == 0.0


# ---------------------------------------------------------------------------
# collect_cycle_metrics — hooks / execution health
# ---------------------------------------------------------------------------

class TestExecutionHealth:
    def test_hooks_passed(self):
        m = collect_cycle_metrics(1, [], [], [], 5.5)
        assert m.hooks_passed is True
        assert m.hook_failure_count == 0
        assert m.elapsed_s == 5.5

    def test_hooks_failed(self):
        m = collect_cycle_metrics(1, [], [], ["syntax error in foo.py"], 0.0)
        assert m.hooks_passed is False
        assert m.hook_failure_count == 1


# ---------------------------------------------------------------------------
# collect_cycle_metrics — redundancy
# ---------------------------------------------------------------------------

class TestRedundancy:
    def test_no_redundant_reads(self):
        log = [
            _entry("batch_read", inp={"paths": ["a.py"]}),
            _entry("batch_read", inp={"paths": ["b.py"]}),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.redundant_reads == 0
        assert m.redundant_read_rate == 0.0

    def test_redundant_batch_read(self):
        log = [
            _entry("batch_read", inp={"paths": ["a.py"]}),
            _entry("batch_read", inp={"paths": ["a.py", "b.py"]}),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.redundant_reads == 1  # a.py read twice

    def test_redundant_read_file(self):
        log = [
            _entry("read_file", inp={"path": "x.py"}),
            _entry("read_file", inp={"path": "x.py"}),
            _entry("read_file", inp={"path": "y.py"}),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.redundant_reads == 1
        assert m.redundant_read_rate == pytest.approx(0.3333, abs=1e-4)

    def test_dict_style_paths(self):
        log = [
            _entry("batch_read", inp={"paths": [{"path": "a.py"}, {"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.redundant_reads == 1


# ---------------------------------------------------------------------------
# collect_cycle_metrics — context quality
# ---------------------------------------------------------------------------

class TestContextQuality:
    def test_all_reads_used(self):
        """Read a.py and b.py, edited both → hit rate 100%."""
        log = [
            _entry("batch_read", inp={"paths": ["a.py", "b.py"]}),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
            _entry("batch_edit", inp={"edits": [{"path": "b.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py", "b.py"], [], 0.0)
        assert m.context_files_read == 2
        assert m.context_files_used == 2
        assert m.context_hit_rate == 1.0
        assert m.context_waste_rate == 0.0

    def test_partial_use(self):
        """Read 4 files, only edited 1 → hit rate 25%."""
        log = [
            _entry("batch_read", inp={"paths": ["a.py", "b.py", "c.py", "d.py"]}),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.context_files_read == 4
        assert m.context_files_used == 1
        assert m.context_hit_rate == 0.25
        assert m.context_waste_rate == 0.75

    def test_no_reads(self):
        """No reads at all — 0/0, rates stay 0."""
        m = collect_cycle_metrics(1, [_entry("bash")], ["x.py"], [], 0.0)
        assert m.context_files_read == 0
        assert m.context_hit_rate == 0.0
        assert m.context_waste_rate == 0.0

    def test_read_file_tool(self):
        """read_file (single-file variant) also counted."""
        log = [
            _entry("read_file", inp={"path": "x.py"}),
            _entry("batch_edit", inp={"edits": [{"path": "x.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["x.py"], [], 0.0)
        assert m.context_files_read == 1
        assert m.context_files_used == 1
        assert m.context_hit_rate == 1.0

    def test_dict_style_paths(self):
        log = [
            _entry("batch_read", inp={"paths": [{"path": "a.py"}, {"path": "b.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.context_files_read == 2
        assert m.context_files_used == 1


# ---------------------------------------------------------------------------
# collect_cycle_metrics — memory & learning
# ---------------------------------------------------------------------------

class TestMemoryLearning:
    def test_notes_consulted_batch_read(self):
        log = [
            _entry("batch_read", inp={"paths": ["agent_notes.md"]}),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.notes_consulted is True

    def test_notes_consulted_read_file(self):
        log = [
            _entry("read_file", inp={"path": "/run/agent_notes.md"}),
        ]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.notes_consulted is True

    def test_notes_not_consulted(self):
        log = [_entry("batch_read", inp={"paths": ["src/main.py"]})]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.notes_consulted is False

    def test_plan_before_act_good(self):
        """Read → search → edit = plan before act."""
        log = [
            _entry("batch_read", inp={"paths": ["a.py"]}),
            _entry("grep_search"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.plan_before_act is True

    def test_plan_before_act_bad(self):
        """Edit is the very first call = no planning."""
        log = [
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
            _entry("batch_read", inp={"paths": ["b.py"]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.plan_before_act is False

    def test_plan_before_act_bash_before_write(self):
        """bash before first write breaks plan_before_act."""
        log = [
            _entry("batch_read"),
            _entry("bash"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.plan_before_act is False

    def test_plan_before_act_scratchpad_ok(self):
        """scratchpad before first write is fine (it's planning)."""
        log = [
            _entry("batch_read"),
            _entry("scratchpad"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.plan_before_act is True

    def test_plan_before_act_no_writes(self):
        """No writes at all → vacuously true (exploration only)."""
        log = [_entry("batch_read"), _entry("grep_search")]
        m = collect_cycle_metrics(1, log, [], [], 0.0)
        assert m.plan_before_act is True

    def test_test_after_edit_good(self):
        """edit → test = verified."""
        log = [
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
            _entry("test_runner"),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.test_after_edit is True

    def test_test_after_edit_bad(self):
        """test → edit (no final test) = not verified."""
        log = [
            _entry("test_runner"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.test_after_edit is False

    def test_edit_test_cycles(self):
        """edit → test → edit → test = 2 cycles."""
        log = [
            _entry("batch_read"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
            _entry("test_runner"),
            _entry("batch_edit", inp={"edits": [{"path": "a.py"}]}),
            _entry("test_runner"),
        ]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.edit_test_cycles == 2
        assert m.test_after_edit is True

    def test_edit_test_cycles_zero(self):
        """No test_runner at all → 0 cycles."""
        log = [_entry("batch_edit", inp={"edits": [{"path": "a.py"}]})]
        m = collect_cycle_metrics(1, log, ["a.py"], [], 0.0)
        assert m.edit_test_cycles == 0
        assert m.test_after_edit is False


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

class TestSerialisation:
    def test_metrics_to_dict_roundtrip(self):
        m = collect_cycle_metrics(3, [_entry("bash")], ["f.py"], [], 1.0)
        d = metrics_to_dict(m)
        assert d["cycle"] == 3
        assert d["bash_calls"] == 1
        assert isinstance(json.dumps(d), str)  # JSON-safe

    def test_metrics_to_dict_keys(self):
        m = CycleMetrics(cycle=1)
        d = metrics_to_dict(m)
        assert "total_tool_calls" in d
        assert "redundant_read_rate" in d
        assert "tool_distribution" in d
        assert "context_hit_rate" in d
        assert "context_waste_rate" in d
        assert "notes_consulted" in d
        assert "plan_before_act" in d
        assert "test_after_edit" in d
        assert "edit_test_cycles" in d


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_detailed_report_contains_sections(self):
        m = collect_cycle_metrics(
            2,
            [_entry("batch_read"), _entry("bash", is_error=True)],
            ["x.py"],
            ["syntax failed"],
            12.3,
        )
        report = format_detailed_report(m)
        assert "# Cycle 2" in report
        assert "Tool Efficiency" in report
        assert "Output Quality" in report
        assert "Execution Health" in report
        assert "Redundancy" in report
        assert "Behaviour Signals" in report
        assert "Context Quality" in report
        assert "Memory & Learning" in report
        assert "NO" in report  # hooks failed

    def test_summary_one_line(self):
        m = collect_cycle_metrics(5, [_entry("batch_read")] * 10, ["a.py"], [], 8.0)
        s = format_summary(m)
        assert s.startswith("[metrics]")
        assert "cycle=5" in s
        assert "tools=10" in s
        assert "ctx_hit=" in s
        assert "notes=" in s
        assert "plan=" in s
        assert "test=" in s
        assert "PASS" in s
        assert "\n" not in s

    def test_summary_fail_status(self):
        m = collect_cycle_metrics(1, [], [], ["fail"], 0.0)
        s = format_summary(m)
        assert "FAIL" in s


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_persist_writes_two_files(self, tmp_path):
        m = collect_cycle_metrics(1, [_entry("bash")], [], [], 1.0)
        written: dict[str, str] = {}

        def mock_write(content: str, *segments: str) -> None:
            written["/".join(segments)] = content

        persist_cycle_metrics(m, mock_write, "cycle_1")
        assert "cycle_1/metrics.json" in written
        assert "cycle_1/metrics_report.md" in written

        # JSON is valid
        data = json.loads(written["cycle_1/metrics.json"])
        assert data["cycle"] == 1

        # Markdown has header
        assert "# Cycle 1" in written["cycle_1/metrics_report.md"]
