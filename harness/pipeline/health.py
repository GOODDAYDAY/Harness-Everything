"""Health monitoring for pipeline operational quality.

Tracks key operational metrics and provides early warning signals for
performance degradation, resource issues, and operational readiness.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class HealthMetric:
    """A single health metric measurement."""
    name: str
    value: float
    timestamp: datetime = field(default_factory=datetime.now)
    unit: str = ""
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    name: str
    status: str  # "healthy", "warning", "critical"
    message: str
    metrics: List[HealthMetric] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


class HealthMonitor:
    """Monitors pipeline health and provides early warning signals.
    
    Tracks:
    - Performance trends (response times, throughput)
    - Error rates and patterns
    - Resource usage (memory, tool calls)
    - Operational readiness
    """
    
    def __init__(self, config: Any) -> None:
        self.config = config
        self.metrics_history: List[HealthMetric] = []
        self.max_history_size = 1000
        
    def record_metric(self, name: str, value: float, unit: str = "", **tags: str) -> None:
        """Record a health metric."""
        metric = HealthMetric(
            name=name,
            value=value,
            unit=unit,
            tags=tags
        )
        self.metrics_history.append(metric)
        
        # Keep history size bounded
        if len(self.metrics_history) > self.max_history_size:
            self.metrics_history = self.metrics_history[-self.max_history_size:]
    
    def check_performance_trends(self) -> HealthCheckResult:
        """Check for performance degradation trends."""
        recent_metrics = [
            m for m in self.metrics_history 
            if m.name == "phase_duration_s" and m.tags.get("phase_type") == "implementation"
        ][-10:]  # Last 10 implementation phases
        
        if len(recent_metrics) < 3:
            return HealthCheckResult(
                name="performance_trends",
                status="healthy",
                message="Insufficient data for trend analysis"
            )
        
        # Calculate moving average and check for degradation
        values = [m.value for m in recent_metrics]
        avg_recent = sum(values[-3:]) / 3
        avg_previous = sum(values[:-3]) / (len(values) - 3) if len(values) > 3 else avg_recent
        
        if avg_recent > avg_previous * 1.5:  # 50% increase
            return HealthCheckResult(
                name="performance_trends",
                status="warning",
                message=f"Performance degradation detected: recent avg {avg_recent:.1f}s vs previous {avg_previous:.1f}s",
                metrics=recent_metrics[-3:]
            )
        
        return HealthCheckResult(
            name="performance_trends",
            status="healthy",
            message=f"Performance stable: recent avg {avg_recent:.1f}s",
            metrics=recent_metrics[-3:]
        )
    
    def check_error_rate(self, tool_error_rate: float, threshold: float = 0.1) -> HealthCheckResult:
        """Check if error rate exceeds healthy threshold."""
        self.record_metric("tool_error_rate", tool_error_rate, unit="ratio")
        
        if tool_error_rate > threshold:
            return HealthCheckResult(
                name="error_rate",
                status="critical",
                message=f"High error rate: {tool_error_rate:.1%} exceeds threshold {threshold:.0%}"
            )
        elif tool_error_rate > threshold / 2:
            return HealthCheckResult(
                name="error_rate",
                status="warning",
                message=f"Elevated error rate: {tool_error_rate:.1%}"
            )
        
        return HealthCheckResult(
            name="error_rate",
            status="healthy",
            message=f"Error rate normal: {tool_error_rate:.1%}"
        )
    
    def check_memory_usage(self, memory_entries: int, threshold: int = 1000) -> HealthCheckResult:
        """Check memory usage patterns."""
        self.record_metric("memory_entries", memory_entries, unit="entries")
        
        if memory_entries > threshold:
            return HealthCheckResult(
                name="memory_usage",
                status="warning",
                message=f"High memory usage: {memory_entries} entries exceeds threshold {threshold}"
            )
        
        return HealthCheckResult(
            name="memory_usage",
            status="healthy",
            message=f"Memory usage normal: {memory_entries} entries"
        )
    
    def check_operational_readiness(self, phases_completed: int, phases_total: int) -> HealthCheckResult:
        """Check operational readiness based on completion rate."""
        completion_rate = phases_completed / phases_total if phases_total > 0 else 0
        self.record_metric("completion_rate", completion_rate, unit="ratio")
        
        if completion_rate < 0.5:
            return HealthCheckResult(
                name="operational_readiness",
                status="warning",
                message=f"Low completion rate: {completion_rate:.0%} of phases completed"
            )
        
        return HealthCheckResult(
            name="operational_readiness",
            status="healthy",
            message=f"Completion rate normal: {completion_rate:.0%}"
        )
    
    def run_all_checks(
        self,
        tool_error_rate: float,
        memory_entries: int,
        phases_completed: int,
        phases_total: int
    ) -> List[HealthCheckResult]:
        """Run all health checks and return results."""
        checks = [
            self.check_performance_trends(),
            self.check_error_rate(tool_error_rate),
            self.check_memory_usage(memory_entries),
            self.check_operational_readiness(phases_completed, phases_total)
        ]
        
        # Log any warnings or critical issues
        for check in checks:
            if check.status != "healthy":
                log.warning(
                    "Health check %s: %s - %s",
                    check.name, check.status.upper(), check.message
                )
        
        return checks
    
    def get_health_summary(self) -> Dict[str, Any]:
        """Get a summary of current health status."""
        if not self.metrics_history:
            return {"status": "unknown", "message": "No metrics recorded"}
        
        # Calculate basic statistics
        error_rates = [m.value for m in self.metrics_history if m.name == "tool_error_rate"]
        durations = [m.value for m in self.metrics_history if m.name == "phase_duration_s"]
        
        summary = {
            "status": "healthy",
            "metrics_recorded": len(self.metrics_history),
            "avg_error_rate": sum(error_rates) / len(error_rates) if error_rates else 0,
            "avg_duration": sum(durations) / len(durations) if durations else 0,
            "last_check": datetime.now().isoformat()
        }
        
        return summary