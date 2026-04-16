"""Simple test for evaluator improvements."""

import sys
sys.path.insert(0, '.')

from harness.evaluation.dual_evaluator import parse_score, _MODE_HEADERS


def test_parse_score():
    """Test score parsing functionality."""
    print("Testing parse_score...")
    
    # Test strict parsing
    text = """Some evaluation text.
    SCORE: 7.5
    More text."""
    assert parse_score(text) == 7.5, f"Expected 7.5, got {parse_score(text)}"
    print("✓ Strict parsing works")
    
    # Test loose parsing
    text = """Some evaluation with SCORE = 8.2 in the middle."""
    assert parse_score(text) == 8.2, f"Expected 8.2, got {parse_score(text)}"
    print("✓ Loose parsing works")
    
    # Test clamping
    text = "SCORE: 15.0"
    assert parse_score(text) == 10.0, f"Expected 10.0, got {parse_score(text)}"
    print("✓ Upper clamping works")
    
    text = "SCORE: -5.0"
    assert parse_score(text) == 0.0, f"Expected 0.0, got {parse_score(text)}"
    print("✓ Lower clamping works")
    
    # Test multiple scores (should take last strict)
    text = """SCORE: 5.0
    Some intermediate calculation: SCORE = 3.0
    Final verdict: SCORE: 7.0"""
    assert parse_score(text) == 7.0, f"Expected 7.0, got {parse_score(text)}"
    print("✓ Multiple scores handled correctly")


def test_mode_headers():
    """Test that mode headers contain correct instructions."""
    print("\nTesting mode headers...")
    
    assert "debate" in _MODE_HEADERS, "Missing debate mode"
    assert "implement" in _MODE_HEADERS, "Missing implement mode"
    print("✓ Both modes present")
    
    # Check debate mode mentions text proposals
    debate_text = _MODE_HEADERS["debate"].lower()
    assert "text proposal" in debate_text, "Debate mode should mention text proposals"
    assert "planning round" in debate_text, "Debate mode should mention planning"
    print("✓ Debate mode instructions correct")
    
    # Check implement mode mentions executed code
    implement_text = _MODE_HEADERS["implement"].lower()
    assert "executed code change" in implement_text, "Implement mode should mention executed code"
    assert "code state" in implement_text, "Implement mode should mention code state"
    print("✓ Implement mode instructions correct")


def main():
    """Run all tests."""
    print("=" * 60)
    print("Testing evaluator improvements")
    print("=" * 60)
    
    try:
        test_parse_score()
        test_mode_headers()
        print("\n" + "=" * 60)
        print("All tests passed! ✓")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())