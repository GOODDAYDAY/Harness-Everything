"""Unit tests for harness.evaluation.metrics."""

import math
import pytest

from harness.evaluation.metrics import calculate_critical_range_discrimination


def _filter_and_calculate_std(evaluations, lower=4.0, upper=7.0):
    """
    Filter evaluations for scores within [lower, upper] inclusive and return the sample standard deviation.
    Returns 0.0 if fewer than two valid scores are found.
    """
    scores = []
    for eval_item in evaluations:
        try:
            score = float(eval_item.get("score"))
            if lower <= score <= upper:
                scores.append(score)
        except (TypeError, ValueError):
            continue
    if len(scores) < 2:
        return 0.0
    mean = sum(scores) / len(scores)
    # Use sample variance (dividing by N-1) for consistency with the main function
    variance = sum((s - mean) ** 2 for s in scores) / (len(scores) - 1)
    return math.sqrt(variance)


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


def test_calculate_critical_range_discrimination_two_scores_in_range():
    """Test with two scores in the critical 4-7 range."""
    evaluations = [
        {"score": 5.0},
        {"score": 7.0}
    ]
    result = calculate_critical_range_discrimination(evaluations)
    expected = _filter_and_calculate_std(evaluations)
    assert math.isclose(result, expected, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_mixed_scores():
    """Test with mixed scores inside and outside the critical range."""
    evaluations = [
        {"score": 3.0},  # outside
        {"score": 5.0},  # inside
        {"score": 6.0},  # inside
        {"score": 8.0}   # outside
    ]
    result = calculate_critical_range_discrimination(evaluations)
    expected = _filter_and_calculate_std(evaluations)
    assert math.isclose(result, expected, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_invalid_scores():
    """Test with invalid score values."""
    evaluations = [
        {"score": "not a number"},  # invalid
        {"score": 5.0},             # valid
        {"score": None},            # invalid
        {"score": 6.0}              # valid
    ]
    result = calculate_critical_range_discrimination(evaluations)
    expected = _filter_and_calculate_std(evaluations)
    assert math.isclose(result, expected, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_edge_cases():
    """Test edge cases of the 4-7 range (inclusive)."""
    evaluations = [
        {"score": 4.0},  # lower bound, inside
        {"score": 7.0},  # upper bound, inside
        {"score": 3.999},  # outside
        {"score": 7.001}   # outside
    ]
    result = calculate_critical_range_discrimination(evaluations)
    expected = _filter_and_calculate_std(evaluations)
    assert math.isclose(result, expected, rel_tol=1e-9)


def test_calculate_critical_range_discrimination_multiple_occurrences():
    """Test with multiple scores in the critical range."""
    evaluations = [
        {"score": 4.5},
        {"score": 5.5},
        {"score": 6.5},
        {"score": 4.5}  # duplicate
    ]
    result = calculate_critical_range_discrimination(evaluations)
    expected = _filter_and_calculate_std(evaluations)
    assert math.isclose(result, expected, rel_tol=1e-9)


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


def test__filter_and_calculate_std_edge_inclusive():
    """Test helper's inclusive range filtering."""
    evaluations = [{"score": 4.0}, {"score": 7.0}, {"score": 3.999}, {"score": 7.001}]
    result = _filter_and_calculate_std(evaluations)
    # Should only include 4.0 and 7.0
    # With sample standard deviation (N-1), variance = ((4.0-5.5)**2 + (7.0-5.5)**2) / 1 = 4.5
    expected_std = math.sqrt(((4.0-5.5)**2 + (7.0-5.5)**2) / 1)  # sqrt(4.5) ≈ 2.1213203435596424
    assert math.isclose(result, expected_std, rel_tol=1e-9)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])