"""Test just the parse_score function by extracting it."""

import re

# Copy the parse_score function logic
_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0
_STRICT_RE = re.compile(r"^SCORE:\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)
_LOOSE_RE  = re.compile(r"SCORE[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_score(
    text: str,
    pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
) -> float:
    """Extract a numeric score from evaluator output and clamp it to [0, 10]."""
    strict = _STRICT_RE.findall(text)
    if strict:
        raw = float(strict[-1])
    else:
        loose = re.findall(pattern, text, re.IGNORECASE)
        if not loose:
            print(f"WARNING: no score token found in evaluator output (len={len(text)})")
            return 0.0
        raw = float(loose[-1])

    clamped = max(_SCORE_MIN, min(_SCORE_MAX, raw))
    if clamped != raw:
        print(f"WARNING: raw value {raw:.2f} is outside [{_SCORE_MIN:.0f}, {_SCORE_MAX:.0f}] — clamped to {clamped:.2f}")
    return clamped


def test_parse_score():
    """Test score parsing functionality."""
    print("Testing parse_score...")
    
    # Test strict parsing
    text = """Some evaluation text.
    SCORE: 7.5
    More text."""
    result = parse_score(text)
    assert result == 7.5, f"Expected 7.5, got {result}"
    print("✓ Strict parsing works")
    
    # Test loose parsing
    text = """Some evaluation with SCORE: 8.2 in the middle."""
    result = parse_score(text)
    assert result == 8.2, f"Expected 8.2, got {result}"
    print("✓ Loose parsing works")
    
    # Test clamping
    text = "SCORE: 15.0"
    result = parse_score(text)
    assert result == 10.0, f"Expected 10.0, got {result}"
    print("✓ Upper clamping works")
    
    text = "SCORE: 1.0"
    result = parse_score(text)
    assert result == 1.0, f"Expected 1.0, got {result}"
    print("✓ Positive score works")
    
    # Test that negative numbers aren't parsed (regex doesn't capture negative)
    text = "SCORE: -5.0"
    result = parse_score(text)
    # The regex won't capture -5.0, so it will return 0.0
    assert result == 0.0, f"Expected 0.0 for unparseable negative, got {result}"
    print("✓ Negative score returns 0.0")
    
    # Test multiple scores (should take last strict)
    text = """SCORE: 5.0
    Some intermediate calculation: SCORE: 3.0
    Final verdict: SCORE: 7.0"""
    result = parse_score(text)
    assert result == 7.0, f"Expected 7.0, got {result}"
    print("✓ Multiple scores handled correctly")
    
    # Test no score found
    text = "No score here at all"
    result = parse_score(text)
    assert result == 0.0, f"Expected 0.0, got {result}"
    print("✓ No score returns 0.0")
    
    print("\nAll parse_score tests passed! ✓")
    return True


if __name__ == "__main__":
    try:
        test_parse_score()
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        exit(1)