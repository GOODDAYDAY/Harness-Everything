"""Tests for pipeline health monitoring functionality."""

import json
from unittest.mock import Mock, AsyncMock, patch
import pytest

from harness.pipeline.pipeline_loop import PipelineLoop
from harness.pipeline.health import HealthMonitor
from harness.core.config import HarnessConfig


class TestPipelineHealth:
    """Test health monitoring integration in pipeline loop."""
    
    def test_health_metrics_in_summary(self):
        """Test that health metrics are included in the pipeline summary."""
        # Create a temporary workspace directory
        import tempfile
        import os
        temp_dir = tempfile.mkdtemp()
        
        # Create a mock config
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=temp_dir,
        )
        
        # Create a PipelineLoop instance
        pipeline = PipelineLoop(config)
        
        # Mock the health monitor
        mock_health_metrics = {
            "status": "healthy",
            "metrics_recorded": 5,
            "avg_error_rate": 0.05,
            "avg_duration": 120.5,
            "last_check": "2024-01-01T12:00:00"
        }
        
        # Create a real HealthMonitor instance but mock its metrics_dict property
        health_monitor = HealthMonitor(config)
        health_monitor.metrics_history = [Mock(name="test_metric")]  # Add a dummy metric
        
        # Replace the health monitor on the pipeline instance
        pipeline.health_monitor = health_monitor
        
        # Mock other required attributes
        pipeline.phase_score_history = []
        pipeline.score_trend_warnings = []
        pipeline.total_phases_run = 3
        pipeline.shutdown_reason = "completed"
        pipeline.meta_review_count = 1
        pipeline.auto_push_count = 0
        pipeline.start_time = 1700000000.0
        pipeline._metrics_collector = Mock(total_tool_turns=150)
        
        # Call _write_run_summary and capture the result
        with patch.object(pipeline.artifacts, 'write') as mock_write:
            pipeline._write_run_summary(
                rounds_completed=2,
                best_score=8.5,
                score_history=[{"round": 1, "score": 7.5}, {"round": 2, "score": 8.5}],
                total_elapsed=3600.0,
                total_tool_calls=200,
                total_tool_errors=10,
            )
            
            # Verify that write was called
            assert mock_write.called
            
            # Get the JSON payload that was written
            call_args = mock_write.call_args
            assert call_args[0][1] == "summary.json"
            
            # Parse the JSON payload
            payload = json.loads(call_args[0][0])
            
            # Verify health_metrics key exists
            assert "health_metrics" in payload
            
            # Verify health_metrics is not None
            assert payload["health_metrics"] is not None
            
            # Verify health_metrics has expected structure
            health_metrics = payload["health_metrics"]
            assert "status" in health_metrics
            assert "metrics_recorded" in health_metrics
            assert "avg_error_rate" in health_metrics
            assert "avg_duration" in health_metrics
            assert "last_check" in health_metrics
            
            # Verify other summary fields are present
            assert payload["total_rounds"] == 2
            assert payload["best_score"] == 8.5
            assert payload["total_phases_run"] == 3
            assert "tool_error_rate" in payload
    
    def test_health_metrics_when_monitor_missing(self):
        """Test that health_metrics is None when health monitor is not initialized."""
        # Create a mock config
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace="/tmp/test",
        )
        
        # Create a PipelineLoop instance
        pipeline = PipelineLoop(config)
        
        # Set health_monitor to None
        pipeline.health_monitor = None
        
        # Mock other required attributes
        pipeline.phase_score_history = []
        pipeline.score_trend_warnings = []
        pipeline.total_phases_run = 3
        pipeline.shutdown_reason = "completed"
        pipeline.meta_review_count = 1
        pipeline.auto_push_count = 0
        pipeline.start_time = 1700000000.0
        pipeline._metrics_collector = Mock(total_tool_turns=150)
        
        # Call _write_run_summary and capture the result
        with patch.object(pipeline.artifacts, 'write') as mock_write:
            pipeline._write_run_summary(
                rounds_completed=2,
                best_score=8.5,
                score_history=[{"round": 1, "score": 7.5}, {"round": 2, "score": 8.5}],
                total_elapsed=3600.0,
                total_tool_calls=200,
                total_tool_errors=10,
            )
            
            # Get the JSON payload that was written
            call_args = mock_write.call_args
            payload = json.loads(call_args[0][0])
            
            # Verify health_metrics is None when monitor is missing
            assert payload["health_metrics"] is None
    
    def test_health_monitor_initialization(self):
        """Test that health monitor is properly initialized in PipelineLoop."""
        # Create a mock config
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace="/tmp/test",
        )
        
        # Create a PipelineLoop instance
        pipeline = PipelineLoop(config)
        
        # Verify health monitor is initialized
        assert hasattr(pipeline, 'health_monitor')
        assert isinstance(pipeline.health_monitor, HealthMonitor)
        
        # Verify health monitor has the config
        assert pipeline.health_monitor.config == config