"""Unit tests for harness.evaluation.metrics."""

import math
import pytest

from harness.evaluation.metrics import calculate_critical_range_discrimination


def test_calculate_critical_range_discrimination_empty_list():
    """Test with empty evaluations list."""
    result = calculate_critical_range_discrimination([])
    assert result == 0.0


def test_calculate_critical_range_discrimination_no_scores_in_range():
    """Test with scores outside the critical 4-7 range."""
    evaluations = [
        {"score": 2.0},
        {"score": 3.0},
        {"score": 8.0},
        {"score": 9.0}
    ]
    result = calculate_critical_range_discrimination(evaluations)
    assert result == 0.0


def test_calculate_critical_range_discrimination_single_score_in_range():
    """Test with only one score in the critical 4-7 range."""
    evaluations = [
        {"score": 5.0},
        {"score": 8.0}
    ]
    result = calculate_critical_range_discrimination(evaluations)
    assert result == 0.0


def test_calculate_critical_range_discrimination_two_scores_in_range():
    """Test with two scores in the critical 4-7 range."""
    evaluations = [
        {"score": 5.0},
        {"score": 7.0}
    ]
    result = calculate_critical_range_discrimination(evaluations)
    # Expected standard deviation for [5, 7] is 1.0
    assert math.isclose(result, 1.0, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_mixed_scores():
    """Test with mixed scores inside and outside the critical range."""
    evaluations = [
        {"score": 3.0},  # outside
        {"score": 5.0},  # inside
        {"score": 6.0},  # inside
        {"score": 8.0}   # outside
    ]
    result = calculate_critical_range_discrimination(evaluations)
    # Only consider scores 5 and 6
    # Mean = (5 + 6) / 2 = 5.5
    # Variance = ((5-5.5)^2 + (6-5.5)^2) / 2 = (0.25 + 0.25) / 2 = 0.25
    # Std dev = sqrt(0.25) = 0.5
    assert math.isclose(result, 0.5, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_invalid_scores():
    """Test with invalid score values."""
    evaluations = [
        {"score": "not a number"},  # invalid
        {"score": 5.0},             # valid
        {"score": None},            # invalid
        {"score": 6.0}              # valid
    ]
    result = calculate_critical_range_discrimination(evaluations)
    # Should only consider valid scores 5 and 6
    assert math.isclose(result, 0.5, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_edge_cases():
    """Test edge cases of the 4-7 range (inclusive)."""
    evaluations = [
        {"score": 4.0},  # lower bound, inside
        {"score": 7.0},  # upper bound, inside
        {"score": 3.999},  # outside
        {"score": 7.001}   # outside
    ]
    result = calculate_critical_range_discrimination(evaluations)
    # Only consider scores 4 and 7
    # Mean = (4 + 7) / 2 = 5.5
    # Variance = ((4-5.5)^2 + (7-5.5)^2) / 2 = (2.25 + 2.25) / 2 = 2.25
    # Std dev = sqrt(2.25) = 1.5
    assert math.isclose(result, 1.5, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_multiple_occurrences():
    """Test with multiple scores in the critical range."""
    evaluations = [
        {"score": 4.5},
        {"score": 5.5},
        {"score": 6.5},
        {"score": 4.5}  # duplicate
    ]
    result = calculate_critical_range_discrimination(evaluations)
    # Scores: [4.5, 5.5, 6.5, 4.5]
    # Mean = (4.5 + 5.5 + 6.5 + 4.5) / 4 = 5.25
    # Variance calculation:
    # (4.5-5.25)^2 = 0.5625
    # (5.5-5.25)^2 = 0.0625
    # (6.5-5.25)^2 = 1.5625
    # (4.5-5.25)^2 = 0.5625
    # Sum = 2.75
    # Variance = 2.75 / 4 = 0.6875
    # Std dev = sqrt(0.6875) ≈ 0.82915619758885
    assert math.isclose(result, 0.82915619758885, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_invalid_input_type():
    """Test that calculate_critical_range_discrimination raises TypeError for non-list inputs."""
    # Test with dictionary (non-list)
    with pytest.raises(TypeError, match="evaluations must be a list"):
        calculate_critical_range_discrimination({})
    
    # Test with string (non-list)
    with pytest.raises(TypeError, match="evaluations must be a list"):
        calculate_critical_range_discrimination("not a list")
    
    # Test with integer (non-list)
    with pytest.raises(TypeError, match="evaluations must be a list"):
        calculate_critical_range_discrimination(42)
    
    # Test with None (non-list)
    with pytest.raises(TypeError, match="evaluations must be a list"):
        calculate_critical_range_discrimination(None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])