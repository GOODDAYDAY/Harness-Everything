
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class MockHealthMonitor:
    metrics_dict = {"cpu_usage": 0.5, "memory_mb": 100}

class MockArtifacts:
    def __init__(self):
        self.run_dir = Path("test_output")
        self.run_dir.mkdir(exist_ok=True)

    def write(self, content, *segments):
        path = self.run_dir.joinpath(*segments)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

class MockPipelineLoop:
    def __init__(self):
        self.start_time = time.time()
        self.score_history = [7.5, 8.0, 7.0]
        self.score_trend_warnings = [{"round": 3, "message": "Score declined for 3 consecutive rounds"}]
        self.phase_score_history = [
            {"phase": "requirements", "round": 1, "score": 7.5},
            {"phase": "development", "round": 1, "score": 8.0}
        ]
        self.total_phases_run = 5
        self.shutdown_reason = "completed"
        self.meta_review_count = 2
        self.auto_push_count = 1
        self.health_monitor = MockHealthMonitor()
        self._metrics_collector = type('obj', (object,), {'total_tool_turns': 150})()
        self.artifacts = MockArtifacts()

    def _write_summary_json(self, rounds_completed, best_score, total_tool_calls, total_tool_errors, total_elapsed):
        import datetime

        tool_error_rate = (
            round(total_tool_errors / total_tool_calls, 3)
            if total_tool_calls > 0 else 0.0
        )
        payload = {
            "total_rounds": rounds_completed,
            "best_score": round(best_score, 2),
            "score_history": self.score_history,
            "phase_score_history": self.phase_score_history,
            "score_trend_warnings": self.score_trend_warnings,
            "tool_error_rate": tool_error_rate,
            "total_tool_calls": total_tool_calls,
            "elapsed_total_s": round(total_elapsed, 2),
            "start_time": datetime.datetime.fromtimestamp(self.start_time, datetime.timezone.utc).isoformat(),
            "end_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_phases_run": self.total_phases_run,
            "shutdown_reason": self.shutdown_reason,
            "meta_review_count": self.meta_review_count,
            "auto_push_count": self.auto_push_count,
            "health_metrics": self.health_monitor.metrics_dict if self.health_monitor else None,
        }
        payload["metrics_tool_turns"] = self._metrics_collector.total_tool_turns
        self.artifacts.write(json.dumps(payload, indent=2), "summary.json")
        return payload

# Test the method
loop = MockPipelineLoop()
result = loop._write_summary_json(3, 8.0, 200, 5, 3600.5)

print("Fields written to summary.json:")
for key, value in result.items():
    print(f"  {key}: {type(value).__name__} = {value}")
