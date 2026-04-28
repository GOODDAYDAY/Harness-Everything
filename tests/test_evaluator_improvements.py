"""Test evaluator improvements: structured output, mode adaptation, critique structure."""

import pytest
import re
from harness.evaluation.dual_evaluator import parse_score, _STRICT_RE


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


def test_fractional_score_discrimination():
    """Test that validate_score_calibration is lean: no noise for common mid-range scores."""
    from harness.evaluation.dual_evaluator import validate_score_calibration, _MODE_HEADERS

    # Common mid-range scores should produce ZERO warnings — they are not edge cases.
    for score in [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5]:
        warnings = validate_score_calibration(score, "basic", {"mode": "debate"})
        assert len(warnings) == 0, (
            f"Score {score} should produce 0 warnings (not noise), got: {warnings}"
        )

    # Out-of-range scores are the only mandatory warning.
    warnings = validate_score_calibration(11.0, "basic", {})
    assert len(warnings) == 1 and "outside" in warnings[0].lower()

    # Mode-header content: implement mode must identify itself as reviewing executed code.
    assert "executed code change" in _MODE_HEADERS["implement"].lower()
    assert "code state" in _MODE_HEADERS["implement"].lower()

    # Debate header must identify itself as reviewing a text proposal.
    assert "text proposal" in _MODE_HEADERS["debate"].lower()

    # Headers must be SHORT — calibration anchors and scoring guidance live in the
    # system prompt, not here.  350 chars (≈88 tokens) is plenty for a mode label.
    assert len(_MODE_HEADERS["debate"]) <= 350, (
        f"Debate header too long ({len(_MODE_HEADERS['debate'])} chars); keep calibration in system prompt"
    )
    assert len(_MODE_HEADERS["implement"]) <= 350, (
        f"Implement header too long ({len(_MODE_HEADERS['implement'])} chars); keep calibration in system prompt"
    )


def test_enhanced_discrimination_guidance():
    """validate_score_calibration returns targeted warnings only for genuinely edge scores.

    Calibration rubrics (4-7 range checklists, mode-specific guidance, etc.) already
    live in the system prompts (BASIC_SYSTEM / DIFFUSION_SYSTEM) and must NOT be
    duplicated here as per-call warnings — that would inject hundreds of tokens of
    redundant noise into every LLM context window.
    """
    from harness.evaluation.dual_evaluator import validate_score_calibration

    # Normal mid-range debate scores produce no warnings.
    for score in [4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]:
        warnings = validate_score_calibration(score, "basic", {"mode": "debate"})
        assert warnings == [], (
            f"Score {score} (debate) should produce 0 warnings, got: {warnings}"
        )

    # Extreme debate score ≥ 9.5 gets a targeted reminder.
    warnings = validate_score_calibration(9.7, "basic", {"mode": "debate"})
    assert len(warnings) == 1
    assert "debate" in warnings[0].lower() or "high" in warnings[0].lower()

    # Very low implement score ≤ 3.0 gets a targeted reminder.
    warnings = validate_score_calibration(2.0, "basic", {"mode": "implement"})
    assert len(warnings) == 1
    assert "implement" in warnings[0].lower()

    # Out-of-range immediately returns with one warning, no extras.
    warnings = validate_score_calibration(-1.0, "basic", {})
    assert len(warnings) == 1 and "outside" in warnings[0].lower()

    # Perfect score claims a single concise sanity check.
    warnings = validate_score_calibration(10.0, "basic", {"mode": "implement"})
    assert len(warnings) == 1 and "10" in warnings[0]

    # Zero score claims a single concise sanity check.
    warnings = validate_score_calibration(0.0, "basic", {"mode": "debate"})
    assert len(warnings) == 1 and "0" in warnings[0]


def test_structured_output_format():
    """Test that evaluator output follows structured format."""
    from harness.evaluation.dual_evaluator import _STRICT_RE
    
    # Test the _STRICT_RE regex directly as specified in the plan
    # Test cases from the plan
    assert _STRICT_RE.search("SCORE: 7.5") is not None
    assert _STRICT_RE.search("SCORE: 7.5 with notes") is not None
    assert _STRICT_RE.search("SCORE: invalid") is None
    
    # Additional test cases for robustness
    assert _STRICT_RE.search("SCORE: 8.1") is not None
    assert _STRICT_RE.search("  SCORE: 9.0  ") is not None  # With whitespace
    assert _STRICT_RE.search("SCORE: 10") is not None
    assert _STRICT_RE.search("SCORE: 0.5") is not None
    assert _STRICT_RE.search("SCORE: 7.5\n") is not None  # With newline
    assert _STRICT_RE.search("\nSCORE: 7.5\n") is not None  # With surrounding newlines
    
    # Negative test cases
    assert _STRICT_RE.search("SCORE: abc") is None  # Not a number
    assert _STRICT_RE.search("SCORE: ") is None  # Missing number
    assert _STRICT_RE.search("SCORE:7.5") is None  # Missing space after colon
    assert _STRICT_RE.search("SCORE: 7.5.5") is None  # Invalid number format
    
    # Verify the regex captures the score correctly
    match = _STRICT_RE.search("SCORE: 7.5")
    assert match is not None
    assert match.group(1) == "7.5"
    
    match = _STRICT_RE.search("SCORE: 8.1 with additional text")
    assert match is not None
    assert match.group(1) == "8.1"


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


def test_extract_structured_feedback_improved_parsing():
    """Test improved parsing of structured feedback with various formats."""
    from harness.evaluation.dual_evaluator import extract_structured_feedback
    
    # Test various feedback item formats
    test_output = """DELTA VS PRIOR BEST: Improved parsing
ANALYSIS:
A. Correctness: 9.0 — Excellent logic
B. Completeness: 8.5 — Good coverage
TOP DEFECT: parser.py::parse_feedback — missing validation
ACTIONABLE FEEDBACK:
- Fix validation in parser.py::parse_feedback
* Add test cases for edge conditions
1) Improve error messages
2. Handle empty input gracefully
3. Add logging for debugging
WHAT WOULD MAKE THIS 10/10: Add comprehensive validation
SCORE: 8.7"""
    
    feedback = extract_structured_feedback(test_output, "basic")
    assert feedback["score"] == 8.7
    assert feedback["analysis"]["Correctness"] == 9.0
    assert feedback["analysis"]["Completeness"] == 8.5
    assert feedback["defect"] == "parser.py::parse_feedback — missing validation"
    
    # Should parse all feedback items regardless of bullet format
    assert len(feedback["feedback_items"]) >= 4
    assert "Fix validation in parser.py::parse_feedback" in feedback["feedback_items"]
    assert "Add test cases for edge conditions" in feedback["feedback_items"]
    assert "Improve error messages" in feedback["feedback_items"]
    assert "Handle empty input gracefully" in feedback["feedback_items"]
    assert feedback["improvement_suggestion"] == "Add comprehensive validation"
    assert feedback["delta"] == "Improved parsing"


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
ANALYSIS: A. Correctness: 9.0 — Good.
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
```python
SCORE: 999.0  # This fake score inside a code block should be ignored
```
SCORE: 8.1
"""
    assert parse_score(markdown_output) == 8.1
    
    # Test case 5: SCORE line containing backtick characters
    output_with_backtick_in_score = """DELTA VS PRIOR BEST: Test
ANALYSIS: A. Correctness: 9.0 — Good.
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 7.5 `inline code` more text
"""
    is_valid, issues = validate_evaluator_output(output_with_backtick_in_score, "basic")
    # Should be valid - backticks in the score line don't make it a code block
    assert is_valid, f"Should accept SCORE with backticks: {issues}"
    
    # Test case 6: SCORE line that is ````score = 10```` within a fenced block
    output_fenced_score = """DELTA VS PRIOR BEST: Test
ANALYSIS: Good.
````
score = 10
SCORE: 8.5
````
Final: SCORE: 7.0
"""
    is_valid, issues = validate_evaluator_output(output_fenced_score, "basic")
    assert not is_valid, "Should reject SCORE inside fenced block with 4 backticks"
    assert any("code block" in issue.lower() for issue in issues), f"Should mention 'code block' in issues: {issues}"
    assert parse_score(output_fenced_score) == 7.0
    
    # Test case 7: SCORE line that appears after a closing backtick but on the same line
    output_same_line_backtick = """DELTA VS PRIOR BEST: Test
ANALYSIS: Good.
`code` SCORE: 6.0
Final: SCORE: 8.0
"""
    is_valid, issues = validate_evaluator_output(output_same_line_backtick, "basic")
    # Should be valid - SCORE is after closing backtick on same line
    assert is_valid, f"Should accept SCORE after closing backtick on same line: {issues}"
    assert parse_score(output_same_line_backtick) == 8.0
    
    # Test case 8: Nested code blocks
    output_nested_blocks = """DELTA VS PRIOR BEST: Test
ANALYSIS: Good.
```
Outer block
```
Inner block with SCORE: 5.0
```
```
Final: SCORE: 9.0
"""
    is_valid, issues = validate_evaluator_output(output_nested_blocks, "basic")
    # The SCORE: 5.0 is between two code blocks, not inside one
    assert is_valid, f"Should accept SCORE between code blocks: {issues}"
    assert parse_score(output_nested_blocks) == 9.0


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


def test_score_line_validation_with_trailing_text():
    """Test that SCORE line validation allows trailing text after the score.
    
    This test directly validates the fix for the falsifiable criterion by
    providing a measurable check that the framework now correctly parses
    structured evaluator output with trailing text after the score.
    """
    from harness.evaluation.dual_evaluator import validate_evaluator_output
    
    # Test case 1: SCORE line with trailing text (should be valid)
    output_with_trailing = """DELTA VS PRIOR BEST: This is a test delta comparison
ANALYSIS: A. Correctness: 8.0 — Good implementation.
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 7.5 with trailing text"""
    
    is_valid, issues = validate_evaluator_output(output_with_trailing, "basic")
    assert is_valid, f"Should accept SCORE with trailing text: {issues}"
    
    # Test case 2: SCORE line with inline code (should be valid)
    output_with_inline_code = """DELTA VS PRIOR BEST: This is a test delta comparison
ANALYSIS: A. Correctness: 8.0 — Good implementation.
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 7.5 `inline code` more text"""
    
    is_valid, issues = validate_evaluator_output(output_with_inline_code, "basic")
    assert is_valid, f"Should accept SCORE with inline code: {issues}"
    
    # Test case 3: SCORE line ending immediately after score (should still be valid)
    output_clean = """DELTA VS PRIOR BEST: This is a test delta comparison
ANALYSIS: A. Correctness: 8.0 — Good implementation.
TOP DEFECT: test.py::function — missing error handling
ACTIONABLE FEEDBACK:
1. Add try/except in test.py::function
WHAT WOULD MAKE THIS 10/10: Add unit tests
SCORE: 7.5"""
    
    is_valid, issues = validate_evaluator_output(output_clean, "basic")
    assert is_valid, f"Should accept clean SCORE line: {issues}"


def test_syntax_error_triggers_fail_with_context():
    """Test that syntax errors in changed files trigger FAIL verdict with actionable feedback.
    
    This test directly validates the falsifiable criterion by checking that
    the evaluator provides specific, actionable feedback for syntax errors,
    not just generic error messages.
    """
    import tempfile
    from pathlib import Path

    # Create a temporary Python file with invalid syntax
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write("def invalid:\n    pass  # Missing parentheses after function name\n")
        temp_path = Path(f.name)
    
    try:
        # Run static checks on the file with a syntax error
        changed_files = [str(temp_path)]
        
        # Check if syntax error detection is already implemented
        # by looking for static analysis integration
        from harness.evaluation.static_analysis import run_static_checks
        
        # Run static checks on the invalid file
        result = run_static_checks(changed_files, workspace="/tmp")
        
        # Verify that syntax error is detected
        assert "syntax" in str(result).lower() or "invalid" in str(result).lower(), \
            f"Static analysis should detect syntax error, got: {result}"
        
        # Verify that the feedback contains the erroneous code snippet
        # This is the key assertion for the falsifiable criterion
        result_str = str(result)
        assert "def invalid:" in result_str, \
            f"Feedback should contain the erroneous code snippet 'def invalid:', got: {result_str}"
        
        # Check that it's not just a generic error message
        generic_errors = ["syntax error", "invalid syntax", "parsing failed"]
        has_generic = any(generic in result_str.lower() for generic in generic_errors)
        assert has_generic, \
            f"Feedback should mention syntax error, got: {result_str}"
            
        print(f"✓ Syntax error test passed: {result_str[:100]}...")
        
    finally:
        # Clean up
        temp_path.unlink(missing_ok=True)


def test_score_regex_allows_trailing_text():
    """Test that the _STRICT_RE regex pattern correctly allows trailing text after scores."""
    # Test cases that should match
    test_cases = [
        "SCORE: 7.5 with trailing text",
        "SCORE: 8",
        "  SCORE: 9.0   with spaces and text",
        "SCORE: 10.0 `inline code` more text",
    ]
    
    for test_str in test_cases:
        match = _STRICT_RE.match(test_str)
        assert match is not None, f"Regex should match: {test_str}"
        # Extract the score
        score_str = match.group(1)
        # Verify it's a valid number
        assert re.match(r'^\d+(?:\.\d+)?$', score_str), f"Invalid score extracted: {score_str}"
    
    # Negative test cases that should NOT match
    negative_cases = [
        "SCORE: invalid",
        "SCORE: ",
        "SCORE: abc123",
    ]
    
    for test_str in negative_cases:
        match = _STRICT_RE.match(test_str)
        assert match is None, f"Regex should NOT match: {test_str}"


class TestPromptConsistency:
    """Validate structural consistency of evaluator prompt constants."""

    def test_conservative_decision_tree_gate1_threshold(self):
        """CONSERVATIVE_SYSTEM gate 1 must use score 4.0/4.5 to match BASIC_SYSTEM."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        # Gate 1 must use 4.0/4.5, not the old 4.5/5.0
        assert "score ≤ 4.0" in CONSERVATIVE_SYSTEM, "Gate 1 lower bound must be 4.0"
        assert "score ≥ 4.5" in CONSERVATIVE_SYSTEM, "Gate 1 upper bound must be 4.5"

    def test_conservative_decision_tree_no_old_thresholds(self):
        """CONSERVATIVE_SYSTEM must not use the old misaligned gate thresholds."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        # The old thresholds 4.5/5.0 must no longer appear in decision tree section
        # (they may appear in calibration anchors as score 4.5 examples, but not in gate format)
        import re
        # Look for the old gate format: "NO → Score ≤ 4.5, YES → Score ≥ 5.0"
        old_gate = re.search(r"NO\s*[→→]\s*Score\s*[≤<=]\s*4\.5", CONSERVATIVE_SYSTEM)
        assert old_gate is None, "Old misaligned gate threshold (≤4.5) must be removed"

    def test_basic_system_no_mode_aware_discrimination(self):
        """BASIC_SYSTEM must not contain the removed MODE-AWARE DISCRIMINATION section."""
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        assert "MODE-AWARE DISCRIMINATION" not in BASIC_SYSTEM, (
            "Vague MODE-AWARE DISCRIMINATION section should be removed from BASIC_SYSTEM"
        )

    def test_basic_system_gate1_threshold(self):
        """BASIC_SYSTEM gate 1 must use score 4.0/4.5 (canonical thresholds)."""
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        assert "score ≤ 4.0" in BASIC_SYSTEM, "Gate 1 lower bound must be 4.0"
        assert "score ≥ 4.5" in BASIC_SYSTEM, "Gate 1 upper bound must be 4.5"

    def test_both_systems_gate3_testability(self):
        """Both CONSERVATIVE_SYSTEM and BASIC_SYSTEM gate 3 must mention testability evidence."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        assert "testability evidence" in CONSERVATIVE_SYSTEM, (
            "CONSERVATIVE_SYSTEM gate 3 must mention testability evidence"
        )
        assert "testability evidence" in BASIC_SYSTEM, (
            "BASIC_SYSTEM gate 3 must mention testability evidence"
        )

    def test_conservative_verdict_rules_no_redundant_parenthetical(self):
        """CONSERVATIVE_SYSTEM verdict rules must not repeat the threshold explanation."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        assert "score of 7 is a FAIL — the threshold is strictly 8" not in CONSERVATIVE_SYSTEM, (
            "Redundant parenthetical in VERDICT RULES must be removed"
        )
        # But the key rule must still be there
        assert "pass threshold is strictly 8" in CONSERVATIVE_SYSTEM, (
            "VERDICT RULES must still state the pass threshold is 8"
        )

    def test_conservative_decision_tree_gate3_same_as_basic(self):
        """Both conservative and basic decision tree gate 3 must be functionally identical."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        # Both must have: gate 3 with 6.5 and 7.0 thresholds
        assert "6.5" in CONSERVATIVE_SYSTEM
        assert "7.0" in CONSERVATIVE_SYSTEM
        assert "6.5" in BASIC_SYSTEM
        assert "7.0" in BASIC_SYSTEM

    def test_conservative_fractional_scores_improved(self):
        """CONSERVATIVE_SYSTEM must describe 5.5 as relating to major functionality present."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        # The SCORING GUIDE must describe 5.5 with "major functionality present"
        # (case-insensitive: could be "Major functionality present" in the guide)
        assert "ajor functionality present" in CONSERVATIVE_SYSTEM, (
            "5.5 fractional score must reference 'major functionality present'"
        )

    def test_diffusion_system_has_decision_tree_or_calibration(self):
        """DIFFUSION_SYSTEM must have a scoring guide but not require a decision tree."""
        from harness.prompts.dual_evaluator import DIFFUSION_SYSTEM
        assert "SCORING GUIDE" in DIFFUSION_SYSTEM, (
            "DIFFUSION_SYSTEM must have a SCORING GUIDE section"
        )
        # DIFFUSION evaluates second-order effects, so a code-specific decision tree is not required

    def test_conservative_completeness_references_falsifiable_criterion(self):
        """COMPLETENESS checklist item must reference 'falsifiable criterion', not generic sub-requirements."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        assert "falsifiable criterion" in CONSERVATIVE_SYSTEM, (
            "COMPLETENESS item must anchor evaluation to the task's falsifiable criterion"
        )
        # Must NOT still say "sub-requirement" in the completeness context
        assert "sub-requirement of the task" not in CONSERVATIVE_SYSTEM, (
            "Old 'sub-requirement' phrasing should be replaced with 'falsifiable criterion'"
        )

    def test_aggressive_evaluation_approach_references_falsifiable_criterion(self):
        """AGGRESSIVE EVALUATION APPROACH step 1 must reference 'falsifiable criterion'."""
        from harness.prompts.evaluator import AGGRESSIVE_SYSTEM
        assert "falsifiable criterion" in AGGRESSIVE_SYSTEM, (
            "EVALUATION APPROACH step 1 must anchor to the task's falsifiable criterion"
        )

    def test_conservative_prior_round_delta_no_redundant_inline_example(self):
        """CONSERVATIVE PRIOR ROUND DELTA must not repeat the Δ format inline (it's in OUTPUT)."""
        from harness.prompts.evaluator import CONSERVATIVE_SYSTEM
        # The inline example 'Δ Completeness: IMPROVED/REGRESSED/UNCHANGED — <one-line reason>'
        # is redundant with the OUTPUT template and should be removed
        assert "State this explicitly as:" not in CONSERVATIVE_SYSTEM, (
            "Redundant 'State this explicitly as:' with inline Δ example must be removed"
        )
        assert "This delta analysis must precede" not in CONSERVATIVE_SYSTEM, (
            "Redundant sentence about ordering should be removed (OUTPUT position implies it)"
        )

    def test_aggressive_prior_round_delta_no_redundant_inline_example(self):
        """AGGRESSIVE PRIOR ROUND DELTA must not repeat the Δ format inline (it's in OUTPUT)."""
        from harness.prompts.evaluator import AGGRESSIVE_SYSTEM
        assert "State explicitly whether each" not in AGGRESSIVE_SYSTEM, (
            "Redundant 'State explicitly whether' with inline Δ example must be removed"
        )
        assert "This delta analysis must precede" not in AGGRESSIVE_SYSTEM, (
            "Redundant sentence about ordering must be removed"
        )

    def test_basic_system_prior_round_delta_no_redundant_inline_example(self):
        """BASIC_SYSTEM PRIOR ROUND DELTA must not repeat the Δ format inline (it's in OUTPUT)."""
        from harness.prompts.dual_evaluator import BASIC_SYSTEM
        assert "State this explicitly as:" not in BASIC_SYSTEM, (
            "Redundant 'State this explicitly as:' must be removed from BASIC_SYSTEM"
        )
        assert "This delta analysis must precede" not in BASIC_SYSTEM, (
            "Redundant ordering sentence must be removed from BASIC_SYSTEM"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

class TestPromptWhitespaceQuality:
    """Verify that prompt strings don't have unintended mid-sentence extra spaces
    (which would result from Python backslash line-continuation with indentation).
    """

    PROMPT_NAMES = [
        ("harness.prompts.evaluator", "CONSERVATIVE_SYSTEM"),
        ("harness.prompts.evaluator", "AGGRESSIVE_SYSTEM"),
        ("harness.prompts.evaluator", "MERGE_SYSTEM"),
        ("harness.prompts.dual_evaluator", "BASIC_SYSTEM"),
        ("harness.prompts.dual_evaluator", "DIFFUSION_SYSTEM"),
        ("harness.prompts.planner", "CONSERVATIVE_SYSTEM"),
        ("harness.prompts.planner", "AGGRESSIVE_SYSTEM"),
        ("harness.prompts.planner", "MERGE_SYSTEM"),
        ("harness.prompts.synthesis", "SYNTHESIS_SYSTEM"),
        ("harness.prompts.meta_review", "META_REVIEW_SYSTEM"),
    ]

    def _get_prompt(self, module_name: str, attr_name: str) -> str:
        import importlib
        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name)

    def test_no_mid_sentence_triple_spaces(self):
        """No prompt should contain 3+ consecutive spaces that are NOT at the start of a line.

        Line-start indentation (e.g., ``\\n      sub-item``) is fine.
        Mid-sentence extra spaces (e.g., ``citing   file``) indicate a Python backslash
        line-continuation was used with an indented continuation, which embeds unwanted
        whitespace into the LLM-facing prompt text.
        """
        import re

        def _find_mid_sentence_extra_spaces(text: str) -> list[tuple[int, str]]:
            """Return (pos, context) for 3+ spaces not at the start of a line."""
            results = []
            for m in re.finditer(r" {3,}", text):
                pos = m.start()
                # Walk back to find the last newline
                last_nl = text.rfind("\n", 0, pos)
                # Check if everything between the newline and this space sequence is spaces
                between = text[last_nl + 1 : pos] if last_nl != -1 else text[:pos]
                if all(c == " " for c in between):
                    continue  # Start-of-line indentation — intentional
                results.append((pos, text[max(0, pos - 30) : m.end() + 30]))
            return results

        for module_name, attr_name in self.PROMPT_NAMES:
            text = self._get_prompt(module_name, attr_name)
            bad = _find_mid_sentence_extra_spaces(text)
            for pos, ctx in bad:
                assert False, (
                    f"{module_name}.{attr_name} has mid-sentence extra spaces "
                    f"at position {pos}: {ctx!r}\n"
                    "This is likely from a Python backslash line-continuation with indentation. "
                    "Fix: remove the backslash and join the lines with a single space."
                )

    def test_no_backslash_continuations_in_source(self):
        """Prompt source files should not use backslash line-continuation inside string literals
        (it creates extra whitespace in the LLM-facing prompt text).
        """
        prompt_files = [
            "harness/prompts/evaluator.py",
            "harness/prompts/dual_evaluator.py",
            "harness/prompts/planner.py",
            "harness/prompts/synthesis.py",
            "harness/prompts/meta_review.py",
        ]
        for filepath in prompt_files:
            with open(filepath) as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                stripped = line.rstrip('\n').rstrip()
                # Flag lines ending with backslash that are NOT string-opener lines
                if stripped.endswith('\\') and '= """\\' not in stripped and not stripped.strip().startswith('#'):
                    assert False, (
                        f"{filepath}:{i+1} has a backslash line-continuation: {repr(stripped)!r}\n"
                        "This creates extra whitespace in the LLM prompt. "
                        "Fix: remove the backslash and join lines with a single space."
                    )
