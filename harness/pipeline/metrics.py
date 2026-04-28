"""harness.metrics — lightweight per-phase execution metrics."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.pipeline.phase import InnerResult, PhaseResult

log = logging.getLogger(__name__)


@dataclass
class InnerRoundMetrics:
    phase: str
    round_index: int
    tool_calls: int
    verdict: str          # e.g. "pass" / "fail" / "error"
    feedback_snippet: str  # first 200 chars of verdict feedback


@dataclass
class PhaseMetrics:
    phase_name: str
    inner_rounds: int
    best_inner_round: int          # 0-indexed
    best_combined_score: float
    test_passed: bool
    tool_turn_counts: list[int]    # one entry per inner round
    total_tool_turns: int


@dataclass
class MetricsCollector:
    output_path: Path
    _phases: list[PhaseMetrics] = field(default_factory=list)
    error_count: int = 0
    _phase_details: list[InnerRoundMetrics] = field(default_factory=list)

    @property
    def total_tool_turns(self) -> int:
        """Sum of tool turns across all recorded phases."""
        return sum(p.total_tool_turns for p in self._phases)

    def record_phase(self, phase_name: str, result: PhaseResult) -> None:
        """Extract statistics from a PhaseResult and append to internal list."""
        inner_rounds: list[InnerResult] = result.inner_results
        if not inner_rounds:
            return

        best_idx = max(
            range(len(inner_rounds)),
            key=lambda i: inner_rounds[i].combined_score,
        )
        best = inner_rounds[best_idx]

        # tool_call_log is populated for implement-mode rounds (list of
        # {"tool": ..., "success": ...} dicts).  Debate-mode rounds leave it
        # empty, so len() == 0 is the correct fallback — no attribute access
        # gymnastics needed.
        tool_turn_counts = [len(r.tool_call_log) for r in inner_rounds]

        pm = PhaseMetrics(
            phase_name=phase_name,
            inner_rounds=len(inner_rounds),
            best_inner_round=best_idx,
            best_combined_score=best.combined_score,
            test_passed=getattr(best, "test_passed", False),
            tool_turn_counts=tool_turn_counts,
            total_tool_turns=sum(tool_turn_counts),
        )
        self._phases.append(pm)
        log.info(
            "metrics: phase=%r rounds=%d best_score=%.3f tool_turns=%d",
            phase_name,
            pm.inner_rounds,
            pm.best_combined_score,
            pm.total_tool_turns,
        )

    def flush(self) -> None:
        """Write collected metrics to output_path atomically via temp-file rename."""
        payload: dict[str, Any] = {
            "phases": [asdict(p) for p in self._phases],
            "totals": {
                "phases_completed": len(self._phases),
                "total_tool_turns": self.total_tool_turns,
            },
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self.output_path.parent, prefix=".metrics_tmp_"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.output_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def record_phase_detail(self, detail: InnerRoundMetrics) -> None:
        """Record per-inner-round metrics for post-run analysis.

        Increments error_count when the detail verdict is 'error'.
        """
        self._phase_details.append(detail)
        if detail.verdict == "error":
            self.error_count += 1

    def flush_detail(self, path: str) -> None:
        """Write accumulated InnerRoundMetrics to *path* as newline-delimited JSON.

        Silently no-ops when no details have been recorded.
        """
        if not self._phase_details:
            return
        with open(path, "w", encoding="utf-8") as fh:
            for d in self._phase_details:
                fh.write(json.dumps(asdict(d)) + "\n")
