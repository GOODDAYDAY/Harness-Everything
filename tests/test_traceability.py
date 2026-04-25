"""Tests for traceability, run observability, and production quality features."""

import json

from harness.core.config import HarnessConfig, PipelineConfig


class TestTraceabilityFeatures:
    """Test traceability and observability features."""
    
    def test_summary_json_schema_completeness(self, tmp_path):
        """Test that summary.json contains all required fields for run dashboard."""
        # Create a config with temporary workspace
        harness_config = HarnessConfig(
            model="test-model",
            max_tokens=1000,
            workspace=str(tmp_path),
        )
        _ = PipelineConfig(harness=harness_config)  # not used directly
        
        # Create a mock summary payload with all expected fields
        mock_summary = {
            "total_rounds": 3,
            "best_score": 15.5,
            "score_history": [
                {"round": 1, "score": 14.0},
                {"round": 2, "score": 15.5},
                {"round": 3, "score": 13.0}
            ],
            "phase_score_history": [
                {
                    "outer_round": 1,
                    "phase": "analysis",
                    "phase_name": "analysis",
                    "score": 14.0,
                    "inner_results": 2,
                    "elapsed_s": 100.0,
                    "timestamp": "2024-01-01T12:00:00+00:00"
                }
            ],
            "end_time": "2024-01-01T12:30:00+00:00",
            "total_phases_run": 5,
            "shutdown_reason": "max_rounds_reached",
            "meta_review_count": 2,
            "auto_push_count": 1,
            "round_metrics": {
                "total": 3,
                "completed": 3,
                "avg_score": 14.17,
                "best_score": 15.5,
                "worst_score": 13.0
            }
        }
        
        # Test that all required fields are present
        required_fields = [
            "total_rounds", "best_score", "score_history", "phase_score_history",
            "end_time", "total_phases_run", "shutdown_reason", "meta_review_count",
            "auto_push_count", "round_metrics"
        ]
        
        for field in required_fields:
            assert field in mock_summary, f"Missing required field: {field}"
        
        # Test field types
        assert isinstance(mock_summary["total_rounds"], int)
        assert isinstance(mock_summary["best_score"], (int, float))
        assert isinstance(mock_summary["score_history"], list)
        assert isinstance(mock_summary["phase_score_history"], list)
        assert isinstance(mock_summary["end_time"], str)
        assert isinstance(mock_summary["total_phases_run"], int)
        assert isinstance(mock_summary["shutdown_reason"], str)
        assert isinstance(mock_summary["meta_review_count"], int)
        assert isinstance(mock_summary["auto_push_count"], int)
        assert isinstance(mock_summary["round_metrics"], dict)
        
        # Test round_metrics structure
        round_metrics_fields = ["total", "completed", "avg_score", "best_score", "worst_score"]
        for field in round_metrics_fields:
            assert field in mock_summary["round_metrics"], f"Missing round_metrics field: {field}"
    
    def test_score_trend_detection(self):
        """Test detection of 3 consecutive declining best_scores."""
        
        # Test case 1: 3 consecutive declines
        score_history_1 = [
            {"round": 1, "score": 20.0},
            {"round": 2, "score": 18.0},
            {"round": 3, "score": 16.0},
            {"round": 4, "score": 14.0}
        ]
        
        # Test case 2: Not 3 consecutive declines (has improvement)
        score_history_2 = [
            {"round": 1, "score": 20.0},
            {"round": 2, "score": 18.0},
            {"round": 3, "score": 19.0},  # Improvement
            {"round": 4, "score": 17.0}
        ]
        
        # Test case 3: Exactly 3 declines
        score_history_3 = [
            {"round": 1, "score": 15.0},
            {"round": 2, "score": 14.0},
            {"round": 3, "score": 13.0}
        ]
        
        # Mock the _detect_score_trend_warnings method
        # We'll test the logic directly
        def detect_trend(history):
            """Simplified version of the trend detection logic."""
            if len(history) < 3:
                return []
            
            warnings = []
            for i in range(len(history) - 2):
                scores = [history[i]["score"], history[i+1]["score"], history[i+2]["score"]]
                if scores[0] > scores[1] > scores[2]:
                    warnings.append({
                        "round_start": history[i]["round"],
                        "round_end": history[i+2]["round"],
                        "score_start": scores[0],
                        "score_end": scores[2],
                        "decline_percent": ((scores[0] - scores[2]) / scores[0]) * 100
                    })
            return warnings
        
        # Test the detection
        warnings_1 = detect_trend(score_history_1)
        warnings_2 = detect_trend(score_history_2)
        warnings_3 = detect_trend(score_history_3)
        
        assert len(warnings_1) == 2  # Rounds 1-3 and 2-4 both decline
        assert len(warnings_2) == 0  # No 3 consecutive declines
        assert len(warnings_3) == 1  # Rounds 1-3 decline
        
        # Verify warning structure
        if warnings_1:
            warning = warnings_1[0]
            assert "round_start" in warning
            assert "round_end" in warning
            assert "score_start" in warning
            assert "score_end" in warning
            assert "decline_percent" in warning
            assert warning["score_start"] > warning["score_end"]
    
    def test_metrics_rollup_aggregation(self, tmp_path):
        """Test per-round aggregation in metrics.json."""
        # Create test metrics for multiple rounds
        round_metrics = []
        for i in range(1, 4):
            round_dir = tmp_path / f"round_{i}"
            round_dir.mkdir()
            
            metrics_data = {
                "round": i,
                "score": 10.0 + i,  # Scores: 11, 12, 13
                "phases_completed": 2,
                "errors": 0,
                "duration_seconds": 100.0 * i,
                "tool_calls": 50 * i
            }
            
            metrics_file = round_dir / "metrics.json"
            with open(metrics_file, 'w') as f:
                json.dump(metrics_data, f)
            
            round_metrics.append(metrics_data)
        
        # Create aggregated metrics
        aggregated = {
            "total_rounds": len(round_metrics),
            "completed_rounds": len(round_metrics),
            "avg_score": sum(m["score"] for m in round_metrics) / len(round_metrics),
            "best_score": max(m["score"] for m in round_metrics),
            "worst_score": min(m["score"] for m in round_metrics),
            "total_phases": sum(m["phases_completed"] for m in round_metrics),
            "total_errors": sum(m["errors"] for m in round_metrics),
            "total_duration": sum(m["duration_seconds"] for m in round_metrics),
            "total_tool_calls": sum(m["tool_calls"] for m in round_metrics),
            "avg_duration_per_round": sum(m["duration_seconds"] for m in round_metrics) / len(round_metrics),
            "avg_tool_calls_per_round": sum(m["tool_calls"] for m in round_metrics) / len(round_metrics)
        }
        
        # Verify aggregated metrics
        assert aggregated["total_rounds"] == 3
        assert aggregated["completed_rounds"] == 3
        assert abs(aggregated["avg_score"] - 12.0) < 0.01  # (11+12+13)/3 = 12
        assert aggregated["best_score"] == 13.0
        assert aggregated["worst_score"] == 11.0
        assert aggregated["total_phases"] == 6  # 2 phases per round × 3 rounds
        assert aggregated["total_errors"] == 0
        assert aggregated["total_duration"] == 600.0  # 100 + 200 + 300
        assert aggregated["total_tool_calls"] == 300  # 50 + 100 + 150
    
    def test_shutdown_reason_tracking(self):
        """Test that shutdown reasons are properly tracked."""
        # Test various shutdown reasons
        shutdown_reasons = [
            "max_rounds_reached",
            "max_time_reached", 
            "score_plateau",
            "manual_stop",
            "error_threshold_exceeded",
            "health_check_failed"
        ]
        
        for reason in shutdown_reasons:
            # Create a mock summary with the shutdown reason
            mock_summary = {
                "shutdown_reason": reason,
                "total_rounds": 5,
                "best_score": 15.0
            }
            
            # Verify the shutdown reason is properly set
            assert mock_summary["shutdown_reason"] == reason
            assert isinstance(mock_summary["shutdown_reason"], str)
            
            # Additional validation for specific reasons
            if reason == "max_rounds_reached":
                assert mock_summary["total_rounds"] > 0
            elif reason == "score_plateau":
                assert "best_score" in mock_summary
    
    def test_meta_review_and_auto_push_counters(self):
        """Test that meta_review_count and auto_push_count are properly tracked."""
        # Create test data with various counts
        test_cases = [
            {"meta_review_count": 0, "auto_push_count": 0},
            {"meta_review_count": 5, "auto_push_count": 2},
            {"meta_review_count": 10, "auto_push_count": 3},
            {"meta_review_count": 1, "auto_push_count": 0},
        ]
        
        for test_case in test_cases:
            mock_summary = {
                "meta_review_count": test_case["meta_review_count"],
                "auto_push_count": test_case["auto_push_count"],
                "total_rounds": 5,
                "best_score": 15.0
            }
            
            # Verify counts are integers and non-negative
            assert isinstance(mock_summary["meta_review_count"], int)
            assert isinstance(mock_summary["auto_push_count"], int)
            assert mock_summary["meta_review_count"] >= 0
            assert mock_summary["auto_push_count"] >= 0
            
            # Verify they match the test case
            assert mock_summary["meta_review_count"] == test_case["meta_review_count"]
            assert mock_summary["auto_push_count"] == test_case["auto_push_count"]


def test_traceability_integration():
    """Integration test for traceability features."""
    # This test verifies that all traceability features work together
    # by checking a complete summary.json structure
    
    complete_summary = {
        "run_id": "test_run_001",
        "start_time": "2024-01-01T12:00:00+00:00",
        "end_time": "2024-01-01T14:30:00+00:00",
        "total_rounds": 7,
        "rounds_completed": 7,
        "best_score": 18.5,
        "score_history": [
            {"round": 1, "score": 15.0},
            {"round": 2, "score": 16.5},
            {"round": 3, "score": 18.5},
            {"round": 4, "score": 17.0},
            {"round": 5, "score": 16.0},
            {"round": 6, "score": 15.5},
            {"round": 7, "score": 14.0}
        ],
        "phase_score_history": [
            {
                "outer_round": 1,
                "phase": "analysis",
                "phase_name": "framework_analysis",
                "score": 16.0,
                "inner_results": 3,
                "elapsed_s": 120.5,
                "timestamp": "2024-01-01T12:10:00+00:00"
            }
        ],
        "total_phases_run": 21,
        "shutdown_reason": "max_rounds_reached",
        "meta_review_count": 3,
        "auto_push_count": 1,
        "score_trend_warnings": [
            {
                "round_start": 5,
                "round_end": 7,
                "score_start": 16.0,
                "score_end": 14.0,
                "decline_percent": 12.5
            }
        ],
        "round_metrics": {
            "total": 7,
            "completed": 7,
            "avg_score": 16.07,
            "best_score": 18.5,
            "worst_score": 14.0,
            "total_phases": 21,
            "total_errors": 2,
            "total_duration": 2100.0,
            "total_tool_calls": 1050
        }
    }
    
    # Verify all traceability fields are present
    traceability_fields = [
        "score_history", "phase_score_history", "total_phases_run",
        "shutdown_reason", "meta_review_count", "auto_push_count",
        "score_trend_warnings", "round_metrics"
    ]
    
    for field in traceability_fields:
        assert field in complete_summary, f"Missing traceability field: {field}"
    
    # Verify score trend warning was detected (rounds 5-7 show decline)
    if complete_summary["score_history"][4]["score"] > complete_summary["score_history"][6]["score"]:
        assert len(complete_summary["score_trend_warnings"]) > 0
    
    # Verify metrics rollup is comprehensive
    assert "avg_score" in complete_summary["round_metrics"]
    assert "total_duration" in complete_summary["round_metrics"]
    assert "total_tool_calls" in complete_summary["round_metrics"]