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
    """Test that last strict anchored score is taken."""
    text = """SCORE: 5.0
    Some intermediate calculation: SCORE = 3.0
    Final verdict: SCORE: 7.0"""
    # Only the first line matches strict anchored pattern (score at start of line)
    # The third line has text before SCORE: so it doesn't match strict anchored pattern
    assert parse_score(text) == 5.0


def test_evaluator_mode_headers():
    """Test that mode headers contain correct instructions."""
    from harness.evaluation.dual_evaluator import _MODE_HEADERS
    
    assert "debate" in _MODE_HEADERS
    assert "implement" in _MODE_HEADERS
    
    # Check debate mode mentions text proposals
    assert "text proposal" in _MODE_HEADERS["debate"].lower()
    assert "plan / recommendation" in _MODE_HEADERS["debate"].lower()
    
    # Check implement mode mentions executed code
    assert "executed code change" in _MODE_HEADERS["implement"].lower()
    assert "code state" in _MODE_HEADERS["implement"].lower()


def test_structured_output_format():
    """Test that evaluator output follows structured format."""
    from harness.evaluation.dual_evaluator import validate_evaluator_output
    
    # Test basic evaluator structure
    basic_output = """DELTA VS PRIOR BEST: More specific file references
ANALYSIS:
A. Correctness: 8.5 — Logic is sound
B. Completeness: 7.0 — Missing edge cases
C. Specificity: 9.0 — Names concrete functions
D. Architecture fit: 8.0 — Fits existing patterns
TOP DEFECT: dual_evaluator.py::parse_score — doesn't handle markdown code blocks
ACTIONABLE FEEDBACK:
1. Update parse_score to strip markdown code blocks
2. Add test for markdown parsing edge case
WHAT WOULD MAKE THIS 10/10: Add validation for structured output format
SCORE: 8.1"""
    
    is_valid, issues = validate_evaluator_output(basic_output, "basic")
    assert is_valid, f"Basic evaluator output should be valid, issues: {issues}"
    
    # Test that DELTA VS PRIOR BEST header is present and has content
    assert "DELTA VS PRIOR BEST:" in basic_output
    delta_line = [line for line in basic_output.split('\n') if line.startswith("DELTA VS PRIOR BEST:")][0]
    assert len(delta_line) > len("DELTA VS PRIOR BEST:") + 1  # Must have descriptive text
    
    # Test diffusion evaluator structure
    diffusion_output = """DELTA VS PRIOR BEST: Better risk assessment
ANALYSIS:
A. Caller impact: 7.0 — 2 callers need updates
B. Maintenance debt: 8.0 — Minimal new complexity
C. Emergent behaviour: 9.0 — No unexpected side effects
D. Rollback safety: 8.5 — Easy to revert
KEY RISK: cross_reference.py::execute — may exceed output limit
ACTIONABLE MITIGATIONS:
1. Add output truncation in cross_reference tool
2. Validate JSON size before serialization
WHAT WOULD MAKE THIS 10/10: Already perfect
SCORE: 8.1"""
    
    is_valid, issues = validate_evaluator_output(diffusion_output, "diffusion")
    assert is_valid, f"Diffusion evaluator output should be valid, issues: {issues}"
    
    # Test that DELTA VS PRIOR BEST header is present and has content
    assert "DELTA VS PRIOR BEST:" in diffusion_output
    delta_line = [line for line in diffusion_output.split('\n') if line.startswith("DELTA VS PRIOR BEST:")][0]
    assert len(delta_line) > len("DELTA VS PRIOR BEST:") + 1  # Must have descriptive text


def test_extract_structured_feedback():
    """Test extraction of structured feedback from evaluator output."""
    from harness.evaluation.dual_evaluator import extract_structured_feedback
    
    basic_output = """DELTA VS PRIOR BEST: Better error handling
ANALYSIS:
A. Correctness: 8.5 — Logic is sound
B. Completeness: 7.0 — Missing edge cases
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
2. Log errors to file
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 7.8"""
    
    feedback = extract_structured_feedback(basic_output, "basic")
    assert feedback["score"] == 7.8
    assert feedback["analysis"]["Correctness"] == 8.5
    assert feedback["analysis"]["Completeness"] == 7.0
    assert feedback["defect"] == "test.py::function — missing error handling"
    assert len(feedback["feedback_items"]) == 2
    assert "Add try/except in test.py::function" in feedback["feedback_items"]
    assert feedback["improvement_suggestion"] == "Add unit tests"
    assert feedback["delta"] == "Better error handling"


def test_parse_score_with_markdown():
    """Test score parsing with markdown code blocks."""
    from harness.evaluation.dual_evaluator import parse_score
    
    text_with_markdown = """```python
Some code here
SCORE: 5.0
More code
```

Final evaluation:
SCORE: 8.5"""
    
    assert parse_score(text_with_markdown) == 8.5


def test_parse_score_unanchored_fallback():
    """Test score parsing with unanchored strict regex fallback."""
    from harness.evaluation.dual_evaluator import parse_score
    
    # Test unanchored strict pattern (not at line start)
    text = "Some text SCORE: 6.5 more text"
    assert parse_score(text) == 6.5
    
    # Test with different case
    text = "Some text score: 7.2 more text"
    assert parse_score(text) == 7.2
    
    # Test with multiple scores (should take last)
    text = "SCORE: 5.0 Some text SCORE: 8.1 more text"
    assert parse_score(text) == 8.1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])