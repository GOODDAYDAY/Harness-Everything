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

from harness.phase import InnerResult, PhaseResult

log = logging.getLogger(__name__)


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

        # tool_turns is accessed via InnerResult.exec_result when present;
        # falls back to 0 if the attribute is not available (debate-mode rounds
        # do not produce an ExecutionResult).
        tool_turn_counts = [
            getattr(getattr(r, "exec_result", None), "tool_turns", 0)
            for r in inner_rounds
        ]

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
                "total_tool_turns": sum(p.total_tool_turns for p in self._phases),
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
