"""Tests for pipeline health monitoring functionality."""

import json
from unittest.mock import Mock, patch, PropertyMock

from harness.pipeline.pipeline_loop import PipelineLoop
from harness.pipeline.health import HealthMonitor
from harness.core.config import HarnessConfig, PipelineConfig


class TestPipelineHealth:
    """Test health monitoring integration in pipeline loop."""
    
    def _assert_health_metrics_present(self, summary_payload, expected_metrics):
        """Verify health metrics are correctly serialized in pipeline summary."""
        assert "health_metrics" in summary_payload
        assert summary_payload["health_metrics"] == expected_metrics
        # Falsifiable criterion validation: structured output improves discrimination
        assert isinstance(summary_payload["health_metrics"]["avg_error_rate"], float), \
            "Health metrics must produce structured numeric data for better discrimination"
    
    def test_health_metrics_in_summary(self, tmp_path):
        """Test that health metrics are included in the pipeline summary."""
        # Create a config with temporary workspace
        harness_config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        config = PipelineConfig(harness=harness_config)
        
        # Create a PipelineLoop instance
        pipeline = PipelineLoop(config)
        
        # Define expected health metrics
        expected_metrics = {
            "status": "healthy",
            "metrics_recorded": 5,
            "avg_error_rate": 0.05,
            "avg_duration": 120.5,
            "last_check": "2024-01-01T12:00:00Z"
        }
        
        # Create a real HealthMonitor instance
        health_monitor = HealthMonitor(config)
        
        # Mock the metrics_dict property to return our expected metrics
        with patch.object(type(health_monitor), 'metrics_dict', new_callable=PropertyMock, return_value=expected_metrics):
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
                
                # Use helper to verify health metrics are correctly serialized
                self._assert_health_metrics_present(payload, expected_metrics)
                
                # Verify other summary fields are present
                assert payload["total_rounds"] == 2
                assert payload["best_score"] == 8.5
                assert payload["total_phases_run"] == 3
                assert "tool_error_rate" in payload
    
    def test_health_metrics_when_monitor_missing(self, tmp_path):
        """Test that health_metrics is None when health monitor is not initialized."""
        # Create a config with temporary workspace
        harness_config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        config = PipelineConfig(harness=harness_config)
        
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
    
    def test_health_monitor_initialization(self, tmp_path):
        """Test that health monitor is properly initialized in PipelineLoop."""
        # Create a mock config using tmp_path for portability
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        
        # Create a PipelineLoop instance
        pipeline = PipelineLoop(config)
        
        # Verify health monitor is initialized
        assert hasattr(pipeline, 'health_monitor')
        assert isinstance(pipeline.health_monitor, HealthMonitor)
        
        # Verify health monitor has the config
        assert pipeline.health_monitor.config == config
    
    def test_health_monitor_metrics_dict_serialization(self, tmp_path):
        """Test that health monitor metrics_dict is properly serialized in pipeline summary."""
        # Create a config with temporary workspace
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        
        # Instantiate a real HealthMonitor with that config
        health_monitor = HealthMonitor(config)
        
        # Define expected metrics that metrics_dict should return
        expected_metrics = {
            "status": "healthy",
            "metrics_recorded": 5,
            "avg_error_rate": 0.05,
            "avg_duration": 120.5,
            "last_check": "2024-01-01T12:00:00Z"
        }
        
        # Mock the metrics_dict property to return our expected metrics
        with patch.object(type(health_monitor), 'metrics_dict', new_callable=PropertyMock, return_value=expected_metrics):
            # Create a PipelineLoop instance
            pipeline = PipelineLoop(config)
            
            # Replace the health monitor on the pipeline instance with our prepared one
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
                
                # Verify health_metrics key exists and matches expected metrics
                assert "health_metrics" in payload
                assert payload["health_metrics"] is not None
                
                # Verify health_metrics exactly equals the expected metrics
                health_metrics = payload["health_metrics"]
                assert health_metrics == expected_metrics
                
                # Additional validation of serialized data types
                assert payload["health_metrics"]["metrics_recorded"] == 5
                assert isinstance(payload["health_metrics"]["avg_duration"], (int, float))
                
                # Verify specific fields match
                assert health_metrics["status"] == "healthy"
                assert health_metrics["metrics_recorded"] == 5
                assert health_metrics["avg_error_rate"] == 0.05
                assert health_metrics["avg_duration"] == 120.5
                assert health_metrics["last_check"] == "2024-01-01T12:00:00Z"
    
    def test_health_monitor_integration_without_mocking(self, tmp_path):
        """Test HealthMonitor integration without mocking - uses real metrics collection."""
        # Create a real HealthMonitor instance
        config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        health_monitor = HealthMonitor(config)
        
        # Record some real metrics
        health_monitor.record_metric("phase_duration_s", 120.0, unit="seconds", phase_type="implementation")
        health_monitor.record_metric("phase_duration_s", 130.0, unit="seconds", phase_type="implementation")
        health_monitor.record_metric("phase_duration_s", 110.0, unit="seconds", phase_type="implementation")
        health_monitor.record_metric("tool_error_rate", 0.05, unit="ratio")
        health_monitor.record_metric("tool_error_rate", 0.03, unit="ratio")
        
        # Get metrics dict through public API (not mocked)
        metrics_dict = health_monitor.metrics_dict
        
        # Verify the structure and content
        assert "status" in metrics_dict
        assert "metrics_recorded" in metrics_dict
        assert "avg_error_rate" in metrics_dict
        assert "avg_duration" in metrics_dict
        assert "last_check" in metrics_dict
        
        # Verify specific values
        assert metrics_dict["metrics_recorded"] == 5
        assert isinstance(metrics_dict["avg_error_rate"], float)
        assert isinstance(metrics_dict["avg_duration"], float)
        
        # Verify the metrics can be serialized to JSON (important for pipeline summary)
        import json
        json_str = json.dumps(metrics_dict)
        deserialized = json.loads(json_str)
        assert deserialized == metrics_dict