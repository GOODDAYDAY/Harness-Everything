"""Test evaluator improvements: structured output, mode adaptation, critique structure."""

import re
import pytest
from harness.evaluation.dual_evaluator import parse_score


def test_parse_score_strict():
    """Test strict score parsing with anchored format."""
    text = """Some evaluation text.
    SCORE: 7.5
    More text."""
    assert parse_score(text) == 7.5


def test_parse_score_loose():
    """Test loose score parsing fallback."""
    text = """Some evaluation with SCORE = 8.2 in the middle."""
    assert parse_score(text) == 8.2


def test_parse_score_clamping():
    """Test score clamping to [0, 10] range."""
    text = "SCORE: 15.0"
    assert parse_score(text) == 10.0
    
    text = "SCORE: -5.0"
    assert parse_score(text) == 0.0


def test_parse_score_multiple():
    """Test that last strict score is taken."""
    text = """SCORE: 5.0
    Some intermediate calculation: SCORE = 3.0
    Final verdict: SCORE: 7.0"""
    assert parse_score(text) == 7.0


def test_evaluator_mode_headers():
    """Test that mode headers contain correct instructions."""
    from harness.evaluation.dual_evaluator import _MODE_HEADERS
    
    assert "debate" in _MODE_HEADERS
    assert "implement" in _MODE_HEADERS
    
    # Check debate mode mentions text proposals
    assert "text proposal" in _MODE_HEADERS["debate"].lower()
    assert "planning round" in _MODE_HEADERS["debate"].lower()
    
    # Check implement mode mentions executed code
    assert "executed code change" in _MODE_HEADERS["implement"].lower()
    assert "code state" in _MODE_HEADERS["implement"].lower()


def test_structured_output_format():
    """Test that evaluator output follows structured format."""
    # This test will be expanded after implementing structured output
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])