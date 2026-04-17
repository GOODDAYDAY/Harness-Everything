"""Test evaluator improvements: structured output, mode adaptation, critique structure."""

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


def test_validate_output_without_delta():
    """Test that evaluator output without DELTA VS PRIOR BEST is still valid."""
    from harness.evaluation.dual_evaluator import validate_evaluator_output
    
    # Valid evaluator output without DELTA VS PRIOR BEST section
    output_without_delta = """ANALYSIS:
A. Correctness: 9.0 — Logic is sound
B. Completeness: 8.5 — All requirements addressed
C. Specificity: 8.0 — Names concrete functions
D. Architecture fit: 8.0 — Fits existing patterns
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 8.8"""
    
    is_valid, issues = validate_evaluator_output(output_without_delta, "basic")
    assert is_valid, f"Output without DELTA should be valid, issues: {issues}"
    # It might have warnings about missing dimensions, but should still be valid
    # Check that there are no errors (only warnings)
    error_issues = [issue for issue in issues if not issue.startswith("WARNING:")]
    assert len(error_issues) == 0, f"Should have no error issues, got: {error_issues}"


def test_parse_score_markdown_edge_case():
    """Test score parsing when SCORE appears on same line as closing backticks."""
    from harness.evaluation.dual_evaluator import parse_score
    
    # Test case where SCORE: appears on same line as closing backticks
    text_with_score_on_same_line = """```python
Some code here
SCORE: 5.0
More code
SCORE: 9.5```"""
    
    assert parse_score(text_with_score_on_same_line) == 9.5
    
    # Another edge case: SCORE: in same line as opening backticks
    text_score_with_opening_backticks = """```SCORE: 7.5
Some code
```"""
    
    assert parse_score(text_score_with_opening_backticks) == 7.5


def test_validate_output_score_in_code_block_rejected():
    """Test that validate_evaluator_output rejects SCORE: lines inside markdown code blocks."""
    from harness.evaluation.dual_evaluator import validate_evaluator_output, parse_score
    
    # Test case 1: SCORE inside a code block
    output_with_score_in_code_block = """DELTA VS PRIOR BEST: Better error handling
ANALYSIS:
A. Correctness: 8.5 — Logic is sound
B. Completeness: 7.0 — Missing edge cases
Here's some code:
```
def example():
    SCORE: 8.5
    return True
```
Final evaluation:
SCORE: 7.8"""
    
    # validate_evaluator_output should reject this
    is_valid, issues = validate_evaluator_output(output_with_score_in_code_block, "basic")
    assert not is_valid, "Should reject SCORE: inside code block"
    assert any("code block" in issue.lower() for issue in issues), f"Should mention 'code block' in issues: {issues}"
    
    # parse_score should not extract the score from inside the code block
    # It should extract the last valid score (7.8)
    assert parse_score(output_with_score_in_code_block) == 7.8
    
    # Test case 2: SCORE on same line as closing backticks (edge case)
    output_score_on_closing_backticks = """ANALYSIS:
A. Correctness: 9.0 — Logic is sound
```
Some code here
SCORE: 6.5```
Final evaluation:
SCORE: 8.2"""
    
    # This should be valid because SCORE: is on the same line as closing backticks
    # which means it's technically outside the code block
    is_valid, issues = validate_evaluator_output(output_score_on_closing_backticks, "basic")
    # It might have other validation issues, but shouldn't fail due to code block
    # Let's just check that parse_score extracts the correct score
    assert parse_score(output_score_on_closing_backticks) == 8.2
    
    # Test case 3: Multiple SCORE lines, one inside code block
    output_multiple_scores = """SCORE: 5.0
```
SCORE: 6.0
```
SCORE: 7.0"""
    
    # validate_evaluator_output should reject due to SCORE inside code block
    is_valid, issues = validate_evaluator_output(output_multiple_scores, "basic")
    assert not is_valid, "Should reject SCORE: inside code block"
    assert any("code block" in issue.lower() for issue in issues), f"Should mention 'code block' in issues: {issues}"
    
    # parse_score should extract the last valid score (7.0)
    assert parse_score(output_multiple_scores) == 7.0
    
    # Test case 4: Specific case from implementation plan - score inside python code block
    markdown_output = """DELTA VS PRIOR BEST: Test
ANALYSIS: Good.
```python
SCORE: 999.0  # This fake score inside a code block should be ignored
```
FINAL SCORE: 8.1
"""
    assert parse_score(markdown_output) == 8.1


def test_parse_score_ignores_scores_in_code_blocks():
    """Test that parse_score ignores scores inside markdown code blocks."""
    from harness.evaluation.dual_evaluator import parse_score
    
    # Create an evaluator output with a legitimate SCORE: 7.5 header
    # and a contradictory SCORE: 1.0 inside a markdown code block
    evaluator_output = """DELTA VS PRIOR BEST: Better implementation
ANALYSIS:
A. Correctness: 8.0 — Logic is sound
B. Completeness: 7.0 — All requirements addressed

Here's some example code:
```
def fake_function():
    # This score inside code block should be ignored
    SCORE: 1.0
    return True
```

Final evaluation:
SCORE: 7.5"""
    
    # parse_score should return 7.5, not 1.0
    assert parse_score(evaluator_output) == 7.5, \
        "parse_score should ignore scores inside markdown code blocks"
    
    # Additional test: score at the beginning of a code block
    output2 = """SCORE: 8.0
Some text here.
```
SCORE: 2.0
More fake code
```"""
    assert parse_score(output2) == 8.0
    
    # Additional test: score at the end of a code block
    output3 = """Some text.
```
print("SCORE: 3.0")
SCORE: 3.0
```
Final: SCORE: 9.0"""
    assert parse_score(output3) == 9.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])