"""Unit tests for MetaReview decision parsing (US-10)."""

import pytest

from harness.agent.agent_eval import (
    MetaReviewDecision,
    _parse_meta_review_decision,
)


def test_parse_continue_decision():
    raw = """\
### Progress Summary
Some progress...

### Score Trend
Scores improving.

```json
{"action": "continue", "reason": "scores are improving"}
```

### Recurring Issues
None.
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is not None
    assert decision.action == "continue"
    assert decision.reason == "scores are improving"
    assert "```json" not in text
    assert "Progress Summary" in text


def test_parse_stop_decision():
    raw = """\
### Direction Adjustment
Nothing left to do.

```json
{"action": "stop", "reason": "3 cycles with no changes and no score improvement"}
```
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is not None
    assert decision.action == "stop"
    assert "3 cycles" in decision.reason


def test_parse_pivot_decision():
    raw = """\
Analysis here.

```json
{"action": "pivot", "reason": "current direction exhausted", "pivot_direction": "Focus on bridge/db/ module — untouched files with high importance"}
```
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is not None
    assert decision.action == "pivot"
    assert "bridge/db/" in decision.pivot_direction


def test_parse_pivot_without_direction_becomes_continue():
    raw = """\
Analysis.

```json
{"action": "pivot", "reason": "need to change direction"}
```
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is not None
    assert decision.action == "continue"
    assert "no direction" in decision.reason.lower()


def test_parse_no_json_block_returns_none():
    raw = """\
### Progress Summary
Some progress.

### Score Trend
Stable.
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is None
    assert text == raw


def test_parse_malformed_json_returns_none():
    raw = """\
Analysis.

```json
{action: continue, reason: broken json}
```
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is None


def test_parse_invalid_action_returns_none():
    raw = """\
```json
{"action": "restart", "reason": "invalid action"}
```
"""
    text, decision = _parse_meta_review_decision(raw)
    assert decision is None


def test_json_block_stripped_from_free_text():
    raw = "Before block.\n\n```json\n{\"action\": \"continue\", \"reason\": \"ok\"}\n```\n\nAfter block."
    text, decision = _parse_meta_review_decision(raw)
    assert decision is not None
    assert "```json" not in text
    assert "Before block." in text
    assert "After block." in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
