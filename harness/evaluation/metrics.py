"""Evaluation metrics for scoring discrimination and analysis."""

import math
from typing import Dict, List


def calculate_critical_range_discrimination(evaluations: List[Dict]) -> float:
    """
    Calculate the standard deviation of scores in the critical 4-7 range.
    
    This metric measures how well the evaluator discriminates between submissions
    in the critical middle range where scoring decisions are most difficult.
    
    Uses SAMPLE standard deviation (dividing by N-1) rather than population
    standard deviation (dividing by N) to provide better discrimination sensitivity
    for small sample sizes typical in the critical range.
    
    Args:
        evaluations: List of evaluation dictionaries, each expected to have a 'score' key.
        
    Returns:
        Sample standard deviation of scores in the 4-7 range (inclusive). Returns 0.0 if
        there are fewer than 2 scores in this range.
    """
    # Type guard: ensure evaluations is a list
    if not isinstance(evaluations, list):
        raise TypeError("evaluations must be a list")
    
    # Extract scores from evaluations
    scores = []
    for eval_dict in evaluations:
        if isinstance(eval_dict, dict) and 'score' in eval_dict:
            try:
                score = float(eval_dict['score'])
                scores.append(score)
            except (ValueError, TypeError):
                # Skip invalid scores
                continue
    
    # Filter to critical range (4-7 inclusive)
    critical_scores = [s for s in scores if 4.0 <= s <= 7.0]
    
    # Need at least 2 scores to calculate standard deviation
    if len(critical_scores) < 2:
        return 0.0
    
    # Calculate mean
    mean = sum(critical_scores) / len(critical_scores)
    
    # Calculate SAMPLE variance (dividing by N-1 for unbiased estimator)
    # This provides better discrimination sensitivity for small sample sizes
    variance = sum((x - mean) ** 2 for x in critical_scores) / (len(critical_scores) - 1)
    
    # Return sample standard deviation
    return math.sqrt(variance)


def _calculate_std_in_range(evaluations, lower=4.0, upper=7.0):
    """
    Filter evaluations for scores within [lower, upper] inclusive and return the sample standard deviation.
    Returns 0.0 if fewer than two valid scores are found.
    
    This is a helper function used by tests to verify the behavior of calculate_critical_range_discrimination.
    
    Args:
        evaluations: List of evaluation dictionaries, each expected to have a 'score' key.
        lower: Lower bound of the score range (inclusive). Default: 4.0
        upper: Upper bound of the score range (inclusive). Default: 7.0
        
    Returns:
        Sample standard deviation of scores in the specified range. Returns 0.0 if
        there are fewer than 2 scores in this range.
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