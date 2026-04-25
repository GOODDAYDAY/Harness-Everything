"""Tests for harness/pipeline/metrics.py and harness/core/signal_util.py.

Both modules have zero test coverage before this file.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers to build test fixtures
# ---------------------------------------------------------------------------

from harness.pipeline.phase import (
    DualScore,
    InnerResult,
    PhaseConfig,
    PhaseResult,
    ScoreItem,
)
from harness.pipeline.metrics import (
    InnerRoundMetrics,
    MetricsCollector,
    PhaseMetrics,
)
from harness.core.signal_util import (
    install_shutdown_handlers,
    uninstall_shutdown_handlers,
)


def _make_dual_score(basic: float = 7.0, diffusion: float = 5.0) -> DualScore:
    return DualScore(
        basic=ScoreItem(score=basic, critique="ok"),
        diffusion=ScoreItem(score=diffusion, critique="ok"),
    )


def _make_inner_result(
    proposal: str = "proposal",
    dual_score: DualScore | None = None,
    tool_call_count: int = 0,
) -> InnerResult:
    log = [{"tool": "bash", "success": True, "duration_ms": 10}] * tool_call_count
    return InnerResult(
        proposal=proposal,
        dual_score=dual_score,
        tool_call_log=log,
    )


def _make_phase_result(
    inner_results: list[InnerResult] | None = None,
) -> PhaseResult:
    cfg = PhaseConfig(name="test_phase", index=0, system_prompt="You are helpful.")
    return PhaseResult(
        phase=cfg,
        synthesis="synth",
        best_score=7.0,
        inner_results=inner_results or [],
    )


# ===========================================================================
# PhaseMetrics dataclass
# ===========================================================================


class TestPhaseMetrics:
    def test_total_tool_turns_computed_from_counts(self):
        pm = PhaseMetrics(
            phase_name="phase1",
            inner_rounds=3,
            best_inner_round=1,
            best_combined_score=8.0,
            test_passed=True,
            tool_turn_counts=[2, 4, 1],
            total_tool_turns=7,
        )
        assert pm.total_tool_turns == 7
        assert pm.phase_name == "phase1"
        assert pm.best_inner_round == 1

    def test_can_be_serialised_with_asdict(self):
        from dataclasses import asdict

        pm = PhaseMetrics(
            phase_name="p",
            inner_rounds=1,
            best_inner_round=0,
            best_combined_score=5.0,
            test_passed=False,
            tool_turn_counts=[3],
            total_tool_turns=3,
        )
        d = asdict(pm)
        assert d["phase_name"] == "p"
        assert d["best_combined_score"] == 5.0
        assert isinstance(d["tool_turn_counts"], list)


# ===========================================================================
# InnerRoundMetrics dataclass
# ===========================================================================


class TestInnerRoundMetrics:
    def test_basic_construction(self):
        irm = InnerRoundMetrics(
            phase="build",
            round_index=2,
            tool_calls=5,
            verdict="pass",
            feedback_snippet="looks good",
        )
        assert irm.phase == "build"
        assert irm.round_index == 2
        assert irm.tool_calls == 5
        assert irm.verdict == "pass"
        assert irm.feedback_snippet == "looks good"

    def test_serialisable_with_asdict(self):
        from dataclasses import asdict

        irm = InnerRoundMetrics(
            phase="p", round_index=0, tool_calls=1, verdict="error", feedback_snippet=""
        )
        d = asdict(irm)
        assert d["verdict"] == "error"


# ===========================================================================
# MetricsCollector.record_phase
# ===========================================================================


class TestMetricsCollectorRecordPhase:
    def _collector(self, tmp_path: Path) -> MetricsCollector:
        return MetricsCollector(output_path=tmp_path / "metrics.json")

    def test_empty_inner_results_skipped(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        result = _make_phase_result(inner_results=[])
        mc.record_phase("phase1", result)
        assert len(mc._phases) == 0

    def test_single_round_recorded(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        ir = _make_inner_result(dual_score=_make_dual_score(8.0, 6.0))
        result = _make_phase_result(inner_results=[ir])
        mc.record_phase("phase1", result)
        assert len(mc._phases) == 1
        pm = mc._phases[0]
        assert pm.phase_name == "phase1"
        assert pm.inner_rounds == 1
        assert pm.best_inner_round == 0

    def test_best_round_by_combined_score(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        # Round 0: combined ~ 0.6*3 + 0.4*3 = 3.0
        # Round 1: combined ~ 0.6*8 + 0.4*6 = 7.2  (best)
        # Round 2: combined ~ 0.6*5 + 0.4*5 = 5.0
        r0 = _make_inner_result(dual_score=_make_dual_score(3.0, 3.0))
        r1 = _make_inner_result(dual_score=_make_dual_score(8.0, 6.0))
        r2 = _make_inner_result(dual_score=_make_dual_score(5.0, 5.0))
        result = _make_phase_result(inner_results=[r0, r1, r2])
        mc.record_phase("phase_x", result)
        pm = mc._phases[0]
        assert pm.best_inner_round == 1
        # 0.6*8 + 0.4*6 = 7.2
        assert abs(pm.best_combined_score - 7.2) < 0.001

    def test_tool_turn_counts_from_log_length(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        r0 = _make_inner_result(tool_call_count=3)
        r1 = _make_inner_result(tool_call_count=5)
        result = _make_phase_result(inner_results=[r0, r1])
        mc.record_phase("phase_y", result)
        pm = mc._phases[0]
        assert pm.tool_turn_counts == [3, 5]
        assert pm.total_tool_turns == 8

    def test_empty_tool_log_gives_zero_turns(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        r = _make_inner_result(tool_call_count=0)
        result = _make_phase_result(inner_results=[r])
        mc.record_phase("phase_z", result)
        pm = mc._phases[0]
        assert pm.tool_turn_counts == [0]
        assert pm.total_tool_turns == 0

    def test_no_dual_score_verdict_pass_gives_10(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        verdict = MagicMock()
        verdict.passed = True
        r = InnerResult(proposal="p", verdict=verdict)
        result = _make_phase_result(inner_results=[r])
        mc.record_phase("phase_v", result)
        pm = mc._phases[0]
        assert pm.best_combined_score == 10.0

    def test_no_dual_score_verdict_fail_gives_zero(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        verdict = MagicMock()
        verdict.passed = False
        r = InnerResult(proposal="p", verdict=verdict)
        result = _make_phase_result(inner_results=[r])
        mc.record_phase("phase_v2", result)
        pm = mc._phases[0]
        assert pm.best_combined_score == 0.0

    def test_no_dual_no_verdict_gives_zero(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        r = InnerResult(proposal="p")
        result = _make_phase_result(inner_results=[r])
        mc.record_phase("phase_none", result)
        pm = mc._phases[0]
        assert pm.best_combined_score == 0.0

    def test_multiple_phases_accumulated(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("p1", _make_phase_result(inner_results=[r]))
        mc.record_phase("p2", _make_phase_result(inner_results=[r, r]))
        assert len(mc._phases) == 2
        assert mc._phases[0].phase_name == "p1"
        assert mc._phases[1].phase_name == "p2"

    def test_total_tool_turns_property(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        r3 = _make_inner_result(tool_call_count=3)
        r5 = _make_inner_result(tool_call_count=5)
        mc.record_phase("p1", _make_phase_result(inner_results=[r3]))
        mc.record_phase("p2", _make_phase_result(inner_results=[r5]))
        assert mc.total_tool_turns == 8


# ===========================================================================
# MetricsCollector.flush
# ===========================================================================


class TestMetricsCollectorFlush:
    def test_flush_writes_json_file(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "metrics.json")
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("phase1", _make_phase_result(inner_results=[r]))
        mc.flush()
        assert (tmp_path / "metrics.json").exists()

    def test_flush_json_structure(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "metrics.json")
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("phase1", _make_phase_result(inner_results=[r]))
        mc.flush()
        data = json.loads((tmp_path / "metrics.json").read_text())
        assert "phases" in data
        assert "totals" in data
        assert data["totals"]["phases_completed"] == 1

    def test_flush_totals_correct(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "metrics.json")
        r3 = _make_inner_result(tool_call_count=3)
        r5 = _make_inner_result(tool_call_count=5)
        mc.record_phase("p1", _make_phase_result(inner_results=[r3]))
        mc.record_phase("p2", _make_phase_result(inner_results=[r5]))
        mc.flush()
        data = json.loads((tmp_path / "metrics.json").read_text())
        assert data["totals"]["phases_completed"] == 2
        assert data["totals"]["total_tool_turns"] == 8

    def test_flush_creates_parent_dirs(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "metrics.json"
        mc = MetricsCollector(output_path=deep)
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("phase1", _make_phase_result(inner_results=[r]))
        mc.flush()
        assert deep.exists()

    def test_flush_empty_phases_still_works(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "metrics.json")
        mc.flush()
        data = json.loads((tmp_path / "metrics.json").read_text())
        assert data["phases"] == []
        assert data["totals"]["phases_completed"] == 0

    def test_flush_phases_contain_phase_names(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "metrics.json")
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("my_phase", _make_phase_result(inner_results=[r]))
        mc.flush()
        data = json.loads((tmp_path / "metrics.json").read_text())
        assert data["phases"][0]["phase_name"] == "my_phase"

    def test_flush_atomic_replace(self, tmp_path: Path):
        """flush() uses atomic rename so the file is never half-written."""
        out = tmp_path / "metrics.json"
        mc = MetricsCollector(output_path=out)
        mc.flush()  # first flush creates the file
        r = _make_inner_result(dual_score=_make_dual_score())
        mc.record_phase("p", _make_phase_result(inner_results=[r]))
        mc.flush()  # second flush overwrites atomically
        data = json.loads(out.read_text())
        assert data["totals"]["phases_completed"] == 1


# ===========================================================================
# MetricsCollector.record_phase_detail + flush_detail
# ===========================================================================


class TestMetricsCollectorDetail:
    def _collector(self, tmp_path: Path) -> MetricsCollector:
        return MetricsCollector(output_path=tmp_path / "m.json")

    def test_record_detail_appends(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        irm = InnerRoundMetrics(
            phase="build", round_index=0, tool_calls=2, verdict="pass", feedback_snippet=""
        )
        mc.record_phase_detail(irm)
        assert len(mc._phase_details) == 1

    def test_record_detail_error_increments_error_count(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        irm = InnerRoundMetrics(
            phase="build", round_index=0, tool_calls=0, verdict="error", feedback_snippet=""
        )
        mc.record_phase_detail(irm)
        assert mc.error_count == 1

    def test_record_detail_non_error_no_increment(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        irm = InnerRoundMetrics(
            phase="build", round_index=0, tool_calls=0, verdict="pass", feedback_snippet=""
        )
        mc.record_phase_detail(irm)
        assert mc.error_count == 0

    def test_flush_detail_writes_ndjson(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        for i in range(3):
            irm = InnerRoundMetrics(
                phase="p", round_index=i, tool_calls=i, verdict="pass", feedback_snippet=""
            )
            mc.record_phase_detail(irm)
        detail_path = str(tmp_path / "detail.ndjson")
        mc.flush_detail(detail_path)
        lines = Path(detail_path).read_text().splitlines()
        assert len(lines) == 3
        # Verify each line is valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert "phase" in parsed
            assert "round_index" in parsed

    def test_flush_detail_noop_when_no_records(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        detail_path = str(tmp_path / "detail.ndjson")
        mc.flush_detail(detail_path)  # should not raise
        assert not Path(detail_path).exists()

    def test_flush_detail_records_all_verdicts(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        for verdict in ["pass", "error", "fail", "pass"]:
            irm = InnerRoundMetrics(
                phase="p", round_index=0, tool_calls=0, verdict=verdict, feedback_snippet="x"
            )
            mc.record_phase_detail(irm)
        detail_path = str(tmp_path / "detail.ndjson")
        mc.flush_detail(detail_path)
        lines = Path(detail_path).read_text().splitlines()
        verdicts = [json.loads(ln)["verdict"] for ln in lines]
        assert verdicts == ["pass", "error", "fail", "pass"]
        assert mc.error_count == 1  # only "error" counted

    def test_multiple_error_verdicts_accumulate(self, tmp_path: Path):
        mc = self._collector(tmp_path)
        for _ in range(4):
            irm = InnerRoundMetrics(
                phase="p", round_index=0, tool_calls=0, verdict="error", feedback_snippet=""
            )
            mc.record_phase_detail(irm)
        assert mc.error_count == 4


# ===========================================================================
# MetricsCollector integration: combined record + flush
# ===========================================================================


class TestMetricsCollectorIntegration:
    def test_full_workflow(self, tmp_path: Path):
        out = tmp_path / "out" / "metrics.json"
        mc = MetricsCollector(output_path=out)

        r1 = _make_inner_result(dual_score=_make_dual_score(9.0, 8.0), tool_call_count=5)
        r2 = _make_inner_result(dual_score=_make_dual_score(6.0, 4.0), tool_call_count=3)
        mc.record_phase("coding", _make_phase_result(inner_results=[r1, r2]))

        irm = InnerRoundMetrics(
            phase="coding", round_index=0, tool_calls=5, verdict="pass", feedback_snippet="good"
        )
        mc.record_phase_detail(irm)

        mc.flush()
        detail_path = str(tmp_path / "detail.ndjson")
        mc.flush_detail(detail_path)

        data = json.loads(out.read_text())
        assert data["totals"]["phases_completed"] == 1
        assert data["totals"]["total_tool_turns"] == 8  # 5 + 3

        phase_data = data["phases"][0]
        assert phase_data["phase_name"] == "coding"
        # 0.6*9 + 0.4*8 = 5.4 + 3.2 = 8.6
        assert abs(phase_data["best_combined_score"] - 8.6) < 0.001

        detail_lines = Path(detail_path).read_text().splitlines()
        assert len(detail_lines) == 1
        assert json.loads(detail_lines[0])["verdict"] == "pass"

    def test_zero_phases_total_tool_turns(self, tmp_path: Path):
        mc = MetricsCollector(output_path=tmp_path / "m.json")
        assert mc.total_tool_turns == 0


# ===========================================================================
# harness/core/signal_util.py
# ===========================================================================


class TestUninstallShutdownHandlers:
    """uninstall_shutdown_handlers() must never raise, even with no loop running."""

    def test_safe_without_running_loop(self):
        """Call uninstall when there is no running event loop — must not raise."""
        # Should silently no-op regardless of loop state
        uninstall_shutdown_handlers()

    def test_idempotent(self):
        """Calling uninstall twice must not raise."""
        uninstall_shutdown_handlers()
        uninstall_shutdown_handlers()


@pytest.mark.skipif(
    sys.platform == "win32", reason="add_signal_handler not available on Windows"
)
class TestInstallShutdownHandlers:
    """install_shutdown_handlers needs a running asyncio event loop."""

    def test_install_and_callback_fires(self):
        """Installing handlers and then sending SIGTERM calls the callback."""
        fired: list[signal.Signals] = []

        async def run():
            # callback takes no arguments
            install_shutdown_handlers(lambda: fired.append(signal.SIGTERM))
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0)  # give the loop a chance to process

        asyncio.run(run())
        assert len(fired) == 1
        assert fired[0] == signal.SIGTERM

    def test_install_sigint_fires(self):
        """SIGINT handler also fires when SIGINT is sent."""
        fired: list[signal.Signals] = []

        async def run():
            install_shutdown_handlers(lambda: fired.append(signal.SIGINT))
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0)

        asyncio.run(run())
        assert len(fired) == 1
        assert fired[0] == signal.SIGINT

    def test_uninstall_prevents_callback(self):
        """After uninstall, sending SIGTERM should NOT call the callback."""
        fired: list[int] = []

        async def run():
            install_shutdown_handlers(lambda: fired.append(1))
            uninstall_shutdown_handlers()
            # After uninstall, default handler is restored — sending SIGTERM
            # to ourselves would terminate the process, so we only test that
            # uninstall runs without errors here.

        asyncio.run(run())
        assert fired == []

    def test_install_without_running_loop_is_noop(self):
        """install_shutdown_handlers with no running loop silently does nothing."""
        # Deliberately call outside of an async context — must not raise
        fired: list[int] = []
        install_shutdown_handlers(lambda: fired.append(1))  # no-op
        assert fired == []
