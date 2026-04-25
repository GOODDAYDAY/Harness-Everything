"""Tests for harness/pipeline/health.py — HealthMonitor."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harness.pipeline.health import HealthMonitor, HealthMetric, HealthCheckResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def monitor():
    config = MagicMock()
    return HealthMonitor(config)


# ---------------------------------------------------------------------------
# HealthMetric / HealthCheckResult data-class sanity
# ---------------------------------------------------------------------------

def test_health_metric_defaults():
    m = HealthMetric(name="foo", value=1.5)
    assert m.name == "foo"
    assert m.value == 1.5
    assert m.unit == ""
    assert m.tags == {}


def test_health_check_result_fields():
    r = HealthCheckResult(name="test", status="healthy", message="all good")
    assert r.status == "healthy"
    assert r.metrics == []


# ---------------------------------------------------------------------------
# record_metric
# ---------------------------------------------------------------------------

def test_record_metric_stores_entry(monitor):
    monitor.record_metric("latency", 1.2, unit="s", phase_type="impl")
    assert len(monitor.metrics_history) == 1
    m = monitor.metrics_history[0]
    assert m.name == "latency"
    assert m.value == 1.2
    assert m.unit == "s"
    assert m.tags["phase_type"] == "impl"


def test_record_metric_bounds_history(monitor):
    for i in range(1500):
        monitor.record_metric("x", float(i))
    # history should be capped at max_history_size (1000)
    assert len(monitor.metrics_history) <= monitor.max_history_size
    # most recent entry is still present
    assert monitor.metrics_history[-1].value == 1499.0


# ---------------------------------------------------------------------------
# check_error_rate
# ---------------------------------------------------------------------------

def test_error_rate_healthy(monitor):
    result = monitor.check_error_rate(0.02)
    assert result.status == "healthy"
    assert "2%" in result.message or "0.0" in result.message or "normal" in result.message.lower()


def test_error_rate_warning(monitor):
    result = monitor.check_error_rate(0.06)  # above threshold/2=0.05
    assert result.status == "warning"


def test_error_rate_critical(monitor):
    result = monitor.check_error_rate(0.5)  # well above threshold=0.1
    assert result.status == "critical"


def test_error_rate_custom_threshold(monitor):
    result = monitor.check_error_rate(0.25, threshold=0.2)
    assert result.status == "critical"


# ---------------------------------------------------------------------------
# check_memory_usage
# ---------------------------------------------------------------------------

def test_memory_healthy(monitor):
    result = monitor.check_memory_usage(100)
    assert result.status == "healthy"


def test_memory_warning(monitor):
    result = monitor.check_memory_usage(1500)  # above default threshold 1000
    assert result.status == "warning"


def test_memory_custom_threshold(monitor):
    result = monitor.check_memory_usage(50, threshold=40)
    assert result.status == "warning"


# ---------------------------------------------------------------------------
# check_operational_readiness
# ---------------------------------------------------------------------------

def test_readiness_healthy(monitor):
    result = monitor.check_operational_readiness(8, 10)
    assert result.status == "healthy"


def test_readiness_warning_low_completion(monitor):
    result = monitor.check_operational_readiness(1, 10)  # 10%
    assert result.status == "warning"


def test_readiness_zero_total(monitor):
    # Should not raise ZeroDivisionError
    result = monitor.check_operational_readiness(0, 0)
    assert result.status in {"healthy", "warning"}


# ---------------------------------------------------------------------------
# check_performance_trends
# ---------------------------------------------------------------------------

def test_performance_insufficient_data(monitor):
    # Only 1 data point — not enough for trend analysis
    monitor.record_metric("phase_duration_s", 1.0, phase_type="implementation")
    result = monitor.check_performance_trends()
    assert result.status == "healthy"
    assert "Insufficient" in result.message


def test_performance_stable_trend(monitor):
    for v in [1.0, 1.1, 0.9, 1.0, 1.2]:
        monitor.record_metric("phase_duration_s", v, phase_type="implementation")
    result = monitor.check_performance_trends()
    assert result.status == "healthy"


def test_performance_degradation(monitor):
    # First 7 fast, last 3 very slow (50%+ increase)
    for _ in range(7):
        monitor.record_metric("phase_duration_s", 1.0, phase_type="implementation")
    for _ in range(3):
        monitor.record_metric("phase_duration_s", 5.0, phase_type="implementation")
    result = monitor.check_performance_trends()
    assert result.status == "warning"


# ---------------------------------------------------------------------------
# check_llm_health
# ---------------------------------------------------------------------------

def test_llm_health_no_calls(monitor):
    result = monitor.check_llm_health()
    assert result.status == "healthy"
    assert "No LLM" in result.message


def test_llm_health_high_success_rate(monitor):
    for _ in range(10):
        monitor.record_llm_metrics("chat", True, tokens_used=1000, duration=1.0)
    result = monitor.check_llm_health()
    assert result.status == "healthy"


def test_llm_health_critical_low_success(monitor):
    for _ in range(5):
        monitor.record_llm_metrics("chat", False, tokens_used=0, duration=0.5)
    for _ in range(5):
        monitor.record_llm_metrics("chat", True, tokens_used=1000, duration=1.0)
    result = monitor.check_llm_health()
    # 50% success rate < 80% threshold → critical
    assert result.status == "critical"


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

def test_run_all_checks_returns_list(monitor):
    results = monitor.run_all_checks(
        tool_error_rate=0.02,
        memory_entries=100,
        phases_completed=9,
        phases_total=10,
    )
    assert isinstance(results, list)
    assert len(results) >= 4  # at minimum 4 checks defined
    names = {r.name for r in results}
    assert "error_rate" in names
    assert "memory_usage" in names
    assert "operational_readiness" in names


def test_run_all_checks_logs_warning(monitor, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="harness.pipeline.health"):
        monitor.run_all_checks(
            tool_error_rate=0.5,  # critical
            memory_entries=100,
            phases_completed=9,
            phases_total=10,
        )
    assert any("CRITICAL" in r.message or "error_rate" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# get_health_summary
# ---------------------------------------------------------------------------

def test_get_health_summary_no_data(monitor):
    summary = monitor.get_health_summary()
    assert summary["status"] == "unknown"


def test_get_health_summary_with_data(monitor):
    monitor.check_error_rate(0.03)
    summary = monitor.get_health_summary()
    assert summary["status"] == "healthy"
    assert summary["metrics_recorded"] >= 1
    assert "avg_error_rate" in summary


# ---------------------------------------------------------------------------
# metrics_dict property
# ---------------------------------------------------------------------------

def test_metrics_dict_property(monitor):
    monitor.record_metric("phase_duration_s", 2.0, phase_type="implementation")
    d = monitor.metrics_dict
    assert isinstance(d, dict)
    assert "metrics_recorded" in d or "status" in d
