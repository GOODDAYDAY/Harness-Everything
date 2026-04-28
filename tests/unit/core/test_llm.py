"""Unit tests for harness.core.llm conversation-management helpers.

Covers:
- _estimate_conversation_chars
- _make_compact_stub
- _compact_old_tool_results
- _prune_conversation_tool_outputs
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.core.llm import (
    _BASH_TEST_PATTERNS,
    _COMPACT_KEEP_RECENT,
    _COMPACT_MIN_TEXT_LEN,
    _COMPACT_PREVIEW_CHARS,
    _HIGH_SIGNAL_PATTERNS,
    _HIGH_SIGNAL_TOOLS,
    _LOW_SIGNAL_PRUNE_TOOLS,
    _MEDIUM_SIGNAL_TOOLS,
    _bash_is_test_output,
    _CachedToolRegistry,
    _compact_old_tool_results,
    _estimate_conversation_chars,
    _make_compact_stub,
    _prune_conversation_tool_outputs,
)
from harness.tools.base import ToolResult


# ---------------------------------------------------------------------------
# Helpers for building conversation fixtures
# ---------------------------------------------------------------------------

def _tool_use_block(tool_id: str, name: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result_block(tool_id: str, text: str) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [{"type": "text", "text": text}],
    }


def _assistant_msg(*blocks: dict) -> dict:
    return {"role": "assistant", "content": list(blocks)}


def _user_result_msg(*blocks: dict) -> dict:
    return {"role": "user", "content": list(blocks)}


def _make_pair(tool_id: str, tool_name: str, result_text: str) -> tuple[dict, dict]:
    """Return (assistant_msg, user_result_msg) for one tool call/result pair."""
    return (
        _assistant_msg(_tool_use_block(tool_id, tool_name)),
        _user_result_msg(_tool_result_block(tool_id, result_text)),
    )


def _build_conversation(
    num_pairs: int,
    result_text: str = "x" * 600,
    tool_name: str = "file_patch",
) -> list[dict]:
    """Build a conversation with *num_pairs* tool-call/result pairs.

    Uses ``file_patch`` as the default tool (default 500-char compaction threshold).
    Pass an explicit *tool_name* to test medium/high-signal tool behaviour.
    """
    msgs: list[dict] = []
    for i in range(num_pairs):
        a, u = _make_pair(f"tid_{i}", tool_name, result_text)
        msgs.append(a)
        msgs.append(u)
    return msgs


# ---------------------------------------------------------------------------
# _estimate_conversation_chars
# ---------------------------------------------------------------------------

class TestEstimateConversationChars:
    def test_empty(self):
        assert _estimate_conversation_chars([]) == 0

    def test_plain_string_content(self):
        conv = [{"role": "user", "content": "hello world"}]
        assert _estimate_conversation_chars(conv) == len("hello world")

    def test_list_text_blocks(self):
        conv = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "text", "text": "de"},
                ],
            }
        ]
        assert _estimate_conversation_chars(conv) == 5

    def test_tool_result_sub_blocks(self):
        conv = [_user_result_msg(_tool_result_block("id1", "result output here"))]
        total = _estimate_conversation_chars(conv)
        assert total == len("result output here")

    def test_multiple_messages(self):
        text_a = "A" * 100
        text_b = "B" * 200
        conv = [
            {"role": "user", "content": text_a},
            _user_result_msg(_tool_result_block("id1", text_b)),
        ]
        assert _estimate_conversation_chars(conv) == 300

    def test_ignores_tool_use_blocks(self):
        conv = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "x", "name": "bash", "input": {}},
                ],
            }
        ]
        # tool_use block has no 'text' key, so should be 0
        assert _estimate_conversation_chars(conv) == 0


# ---------------------------------------------------------------------------
# _make_compact_stub
# ---------------------------------------------------------------------------

class TestMakeCompactStub:
    def test_always_includes_char_count(self):
        text = "x" * 1000
        stub = _make_compact_stub("bash", text)
        assert "1000 chars" in stub
        assert "bash" in stub

    def test_includes_preview(self):
        text = "first line\n" + "more content here\n" * 50
        stub = _make_compact_stub("bash", text)
        assert "first line" in stub

    def test_preview_breaks_at_newline(self):
        preview_len = _COMPACT_PREVIEW_CHARS
        text = "A" * 50 + "\n" + "B" * (preview_len * 2)
        stub = _make_compact_stub("tool", text)
        # Preview should break at the newline
        assert "A" * 50 in stub

    def test_detects_error_signal(self):
        text = "x" * 600 + "\nERROR: something went wrong\n" + "x" * 200
        stub = _make_compact_stub("bash", text)
        assert "ERROR" in stub or "error" in stub.lower()

    def test_detects_traceback_signal(self):
        text = "x" * 600 + "\nTraceback (most recent call last):\n  File foo.py\nValueError\n"
        stub = _make_compact_stub("bash", text)
        assert "traceback" in stub.lower() or "Traceback" in stub

    def test_detects_score_signal(self):
        text = "Normal output line\n" * 100 + "score: 8/10\n"
        stub = _make_compact_stub("evaluator", text)
        assert "score" in stub.lower()

    def test_detects_pass_fail_signal(self):
        text = "x" * 600 + "\n1 failed, 5 passed\n"
        stub = _make_compact_stub("test_runner", text)
        assert "failed" in stub or "passed" in stub

    def test_no_duplicate_signal_lines(self):
        # The signal section deduplicates; put the repetition AFTER the preview window
        # so it can't slip in via preview
        padding = "x" * _COMPACT_PREVIEW_CHARS  # fills the entire preview window
        repeated = "ERROR: boom\n" * 30
        text = padding + "\n" + repeated + "end"
        stub = _make_compact_stub("bash", text)
        # The signal section should only show "ERROR: boom" once
        signal_part = stub.split("signal:", 1)[-1] if "signal:" in stub else stub
        assert signal_part.count("ERROR: boom") == 1

    def test_signal_capped_at_five(self):
        lines = [f"error_{i}: bad thing" for i in range(10)]
        text = "\n".join(lines)
        stub = _make_compact_stub("bash", text)
        signal_part = stub.split("signal:", 1)[-1] if "signal:" in stub else ""
        # At most 5 entries means at most 4 "|" separators
        assert signal_part.count("|") <= 4

    def test_short_text_still_works(self):
        text = "tiny"
        stub = _make_compact_stub("bash", text)
        assert "bash" in stub
        assert "4 chars" in stub

    def test_high_signal_patterns_all_covered(self):
        for kw in _HIGH_SIGNAL_PATTERNS:
            if kw in ("\u2713", "\u2717", "\u2718"):
                line = f"result {kw} ok"
            else:
                line = f"line with {kw} keyword"
            text = "x" * 600 + f"\n{line}\n"
            stub = _make_compact_stub("tool", text)
            assert kw in stub, f"Keyword '{kw}' not found in stub"

    def test_format_has_three_parts(self):
        # Full stub should have the header, preview, and signal sections
        error_line = "ERROR: critical failure"
        text = "first line of preview\n" + "x" * 600 + f"\n{error_line}\n"
        stub = _make_compact_stub("bash", text)
        assert "[bash:" in stub
        assert "preview:" in stub
        assert "signal:" in stub


# ---------------------------------------------------------------------------
# _compact_old_tool_results
# ---------------------------------------------------------------------------

class TestCompactOldToolResults:
    def test_empty_conversation(self):
        assert _compact_old_tool_results([]) == 0

    def test_too_few_pairs_not_compacted(self):
        conv = _build_conversation(_COMPACT_KEEP_RECENT, result_text="x" * 1000)
        count = _compact_old_tool_results(conv)
        assert count == 0
        # Text should be untouched
        for msg in conv:
            if msg["role"] == "user":
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        for sub in block["content"]:
                            assert sub["text"] == "x" * 1000

    def test_recent_pairs_protected(self):
        num_pairs = 6
        conv = _build_conversation(num_pairs, result_text="x" * 1000)
        count = _compact_old_tool_results(conv)
        assert count == (num_pairs - _COMPACT_KEEP_RECENT)

        result_msgs = [m for m in conv if m["role"] == "user"]
        # Last 3 should be untouched
        for msg in result_msgs[-_COMPACT_KEEP_RECENT:]:
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    for sub in block["content"]:
                        assert sub["text"] == "x" * 1000, "Recent pair was wrongly compacted"
        # Old pairs should be compacted
        for msg in result_msgs[:-_COMPACT_KEEP_RECENT]:
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    for sub in block["content"]:
                        assert "compacted" in sub["text"], "Old pair was not compacted"

    def test_short_texts_not_compacted(self):
        short = "x" * (_COMPACT_MIN_TEXT_LEN - 1)
        conv = _build_conversation(8, result_text=short)
        count = _compact_old_tool_results(conv)
        assert count == 0

    def test_compact_stub_contains_tool_name(self):
        conv = _build_conversation(5, result_text="y" * 1000, tool_name="batch_read")
        _compact_old_tool_results(conv)
        result_msgs = [m for m in conv if m["role"] == "user"]
        first = result_msgs[0]
        for block in first["content"]:
            if block.get("type") == "tool_result":
                for sub in block["content"]:
                    assert "batch_read" in sub["text"]

    def test_compact_stub_contains_preview(self):
        preview_trigger = "important context info"
        # Use a generic tool (file_patch, threshold 500) with 1000-char text
        text = preview_trigger + "\n" + "x" * 1000
        conv = _build_conversation(5, result_text=text, tool_name="file_patch")
        _compact_old_tool_results(conv)
        result_msgs = [m for m in conv if m["role"] == "user"]
        first = result_msgs[0]
        for block in first["content"]:
            if block.get("type") == "tool_result":
                for sub in block["content"]:
                    assert "important context info" in sub["text"]

    def test_compact_returns_correct_count(self):
        conv = _build_conversation(8, result_text="z" * 1000)
        count = _compact_old_tool_results(conv)
        assert count == 5  # 8 pairs - 3 kept recent = 5 compacted

    def test_no_tool_result_messages(self):
        conv = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        assert _compact_old_tool_results(conv) == 0

    def test_already_compacted_not_recompacted(self):
        # Run compact twice — second time should return 0 (stubs are small)
        conv = _build_conversation(6, result_text="x" * 1000)
        count1 = _compact_old_tool_results(conv)
        count2 = _compact_old_tool_results(conv)
        assert count1 > 0
        assert count2 == 0  # stubs are now below _COMPACT_MIN_TEXT_LEN

    def test_compact_stub_preserves_error_signal(self):
        text = "x" * 600 + "\nERROR: test suite failed with 3 failures\n"
        conv = _build_conversation(5, result_text=text, tool_name="test_runner")
        _compact_old_tool_results(conv)
        result_msgs = [m for m in conv if m["role"] == "user"]
        # The first (old) result should have the error in the stub
        first = result_msgs[0]
        for block in first["content"]:
            if block.get("type") == "tool_result":
                for sub in block["content"]:
                    assert "error" in sub["text"].lower() or "ERROR" in sub["text"]


# ---------------------------------------------------------------------------
# _prune_conversation_tool_outputs
# ---------------------------------------------------------------------------

class TestPruneConversationToolOutputs:
    def _big_text(self, n: int = 10_000) -> str:
        return "A" * n

    def test_no_pruning_when_under_target(self):
        conv = _build_conversation(4, result_text="small" * 10)
        result_conv, pruned_count, chars_removed = _prune_conversation_tool_outputs(
            conv, target_chars=999_999, keep_recent_pairs=3
        )
        assert pruned_count == 0
        assert chars_removed == 0

    def test_recent_pairs_preserved(self):
        big = self._big_text(50_000)
        conv = _build_conversation(5, result_text=big)

        result_conv, pruned_count, _ = _prune_conversation_tool_outputs(
            conv, target_chars=50_000, keep_recent_pairs=2
        )
        result_msgs = [m for m in result_conv if m["role"] == "user"]
        # Last 2 should be untouched
        for msg in result_msgs[-2:]:
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    for sub in block["content"]:
                        assert sub["text"] == big, "Recent pair was incorrectly pruned"

    def test_pruned_stub_contains_preview(self):
        preview_text = "unique preview content here"
        big = preview_text + "\n" + "x" * 50_000
        conv = _build_conversation(5, result_text=big)

        result_conv, _, _ = _prune_conversation_tool_outputs(
            conv, target_chars=5_000, keep_recent_pairs=1
        )
        result_msgs = [m for m in result_conv if m["role"] == "user"]
        # Old messages should have a compact stub ("compacted") with preview
        found_preview = False
        for msg in result_msgs[:-1]:
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    for sub in block["content"]:
                        # New stub format: "[tool: N chars, compacted]\npreview: ..."
                        if "compacted" in sub["text"]:
                            assert "unique preview content here" in sub["text"]
                            found_preview = True
        assert found_preview, "No compacted stub with preview was found"

    def test_pruned_stub_contains_char_count(self):
        big = "B" * 50_000
        conv = _build_conversation(4, result_text=big)

        result_conv, _, _ = _prune_conversation_tool_outputs(
            conv, target_chars=5_000, keep_recent_pairs=1
        )
        result_msgs = [m for m in result_conv if m["role"] == "user"]
        first = result_msgs[0]
        for block in first["content"]:
            if block.get("type") == "tool_result":
                for sub in block["content"]:
                    assert "50000" in sub["text"]

    def test_chars_removed_positive_when_pruned(self):
        big = self._big_text(50_000)
        conv = _build_conversation(5, result_text=big)

        _, _, chars_removed = _prune_conversation_tool_outputs(
            conv, target_chars=10_000, keep_recent_pairs=2
        )
        assert chars_removed > 0

    def test_pruned_count_positive_when_pruned(self):
        big = self._big_text(50_000)
        conv = _build_conversation(5, result_text=big)

        _, pruned_count, _ = _prune_conversation_tool_outputs(
            conv, target_chars=10_000, keep_recent_pairs=2
        )
        assert pruned_count > 0

    def test_returns_conversation_with_same_structure(self):
        big = self._big_text(50_000)
        conv = _build_conversation(6, result_text=big)
        result_conv, _, _ = _prune_conversation_tool_outputs(
            conv, target_chars=1_000, keep_recent_pairs=2
        )
        assert len(result_conv) == len(conv)
        for orig, pruned in zip(conv, result_conv):
            assert orig["role"] == pruned["role"]

    def test_tiny_results_not_touched(self):
        # Results <=200 chars should not be replaced with stubs
        tiny = "x" * 100
        conv = _build_conversation(8, result_text=tiny)
        result_conv, pruned_count, chars_removed = _prune_conversation_tool_outputs(
            conv, target_chars=1, keep_recent_pairs=1
        )
        assert pruned_count == 0
        assert chars_removed == 0


# ---------------------------------------------------------------------------
# Tool-set membership and pruning threshold consistency
# ---------------------------------------------------------------------------

class TestSignalToolSets:
    """Verify the tool signal sets are mutually exclusive and internally consistent."""

    def test_signal_sets_are_mutually_exclusive(self):
        """No tool should appear in more than one signal category."""
        overlap_high_medium = _HIGH_SIGNAL_TOOLS & _MEDIUM_SIGNAL_TOOLS
        overlap_high_low = _HIGH_SIGNAL_TOOLS & _LOW_SIGNAL_PRUNE_TOOLS
        overlap_medium_low = _MEDIUM_SIGNAL_TOOLS & _LOW_SIGNAL_PRUNE_TOOLS
        assert overlap_high_medium == frozenset(), f"Tools in both HIGH and MEDIUM: {overlap_high_medium}"
        assert overlap_high_low == frozenset(), f"Tools in both HIGH and LOW: {overlap_high_low}"
        assert overlap_medium_low == frozenset(), f"Tools in both MEDIUM and LOW: {overlap_medium_low}"

    def test_bash_is_in_medium_signal_tools(self):
        """bash should be in _MEDIUM_SIGNAL_TOOLS (not high, not low) — it can produce
        large mixed-value output (cat, test runs) so a moderate threshold applies."""
        assert "bash" in _MEDIUM_SIGNAL_TOOLS
        assert "bash" not in _HIGH_SIGNAL_TOOLS
        assert "bash" not in _LOW_SIGNAL_PRUNE_TOOLS

    def test_test_runner_is_in_high_signal_tools(self):
        """test_runner output is always high signal (failure tracebacks, counts)."""
        assert "test_runner" in _HIGH_SIGNAL_TOOLS
        assert "test_runner" not in _MEDIUM_SIGNAL_TOOLS
        assert "test_runner" not in _LOW_SIGNAL_PRUNE_TOOLS

    def test_all_sets_are_frozensets(self):
        """Signal sets must be immutable frozensets."""
        assert isinstance(_HIGH_SIGNAL_TOOLS, frozenset)
        assert isinstance(_MEDIUM_SIGNAL_TOOLS, frozenset)
        assert isinstance(_LOW_SIGNAL_PRUNE_TOOLS, frozenset)


def _build_two_pair_tool_conversation(tool_name: str, old_content: str) -> list[dict]:
    """Build a conversation with 2 tool call/result pairs.

    The first (older) pair uses *tool_name* and *old_content*.  The second
    (recent) pair is a tiny generic tool call.  With keep_recent_pairs=1 the
    second pair is protected and the first is a pruning candidate.
    """
    return [
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        # ---- first (older) pair ----
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "First call."},
                {"type": "tool_use", "id": "t1", "name": tool_name, "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": old_content}],
                }
            ],
        },
        # ---- second (recent, protected) pair ----
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Second call."},
                {"type": "tool_use", "id": "t2", "name": "bash", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": [{"type": "text", "text": "ok"}],
                }
            ],
        },
    ]


def _extract_first_tool_result_text(conv: list[dict]) -> str:
    """Extract the text from the FIRST tool_result in a conversation."""
    for msg in conv:
        if msg.get("role") != "user":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                raw = item.get("content", "")
                if isinstance(raw, list):
                    return raw[0]["text"]
                return str(raw)
    return ""


class TestPruneConversationMediumSignal:
    """_prune_conversation_tool_outputs should apply 1500-char threshold to medium-signal tools.

    Each test builds a two-pair conversation where the FIRST pair is a pruning
    candidate (keep_recent_pairs=1 protects only the second pair).  We check
    whether the first pair's tool result was compacted or kept intact.
    """

    @pytest.mark.parametrize("tool_name", sorted(_MEDIUM_SIGNAL_TOOLS))
    def test_medium_signal_tool_not_pruned_under_1500(self, tool_name):
        """Medium-signal tool output <= 1499 chars should NOT be compacted."""
        content = "x" * 1499
        conv = _build_two_pair_tool_conversation(tool_name, content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" not in text_out, (
            f"Medium-signal tool {tool_name} was compacted unexpectedly at 1499 chars"
        )

    @pytest.mark.parametrize("tool_name", sorted(_MEDIUM_SIGNAL_TOOLS))
    def test_medium_signal_tool_pruned_above_1500(self, tool_name):
        """Medium-signal tool output > 1500 chars should be compacted when target_chars=0."""
        content = "x" * 2000
        conv = _build_two_pair_tool_conversation(tool_name, content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" in text_out, (
            f"Expected 'compacted' in {tool_name} output after pruning, got: {text_out[:200]}"
        )

    @pytest.mark.parametrize("tool_name", sorted(_HIGH_SIGNAL_TOOLS))
    def test_high_signal_tool_not_pruned_under_2000(self, tool_name):
        """High-signal tool output <= 1999 chars should NOT be compacted."""
        content = "x" * 1999
        conv = _build_two_pair_tool_conversation(tool_name, content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" not in text_out, (
            f"High-signal tool {tool_name} was compacted unexpectedly at 1999 chars"
        )

    @pytest.mark.parametrize("tool_name", sorted(_LOW_SIGNAL_PRUNE_TOOLS))
    def test_low_signal_tool_pruned_above_200(self, tool_name):
        """Low-signal tool output > 200 chars should be compacted."""
        content = "x" * 500
        conv = _build_two_pair_tool_conversation(tool_name, content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" in text_out, (
            f"Expected 'compacted' in low-signal {tool_name} output, got: {text_out[:200]}"
        )


# ---------------------------------------------------------------------------
# _bash_is_test_output — unit tests for the content-aware bash detector
# ---------------------------------------------------------------------------

class TestBashIsTestOutput:
    """Tests for the _bash_is_test_output() helper."""

    @pytest.mark.parametrize("text, expected", [
        # Pytest summary lines
        ("3 passed in 0.12s", True),
        ("1 failed, 2 passed in 1.1s", True),
        ("====== FAILURES ======\nsome test\n", True),
        ("------ traceback ------\nFile ...\n", True),
        ("FAILED tests/test_foo.py::test_bar - AssertionError", True),
        ("short test summary info\nFAILED test_x.py::test_y", True),
        # Test ID in output
        ("::test_my_function PASSED", True),
        # Compilation / syntax errors
        ("SyntaxError: invalid syntax at line 5", True),
        ("Traceback (most recent call last):", True),
        ("traceback (most recent call last):", True),
        # Ruff / lint output
        ("ruff check harness/", True),
        ("harness/foo.py:10:5 error[E302] expected blank lines", True),
        ("harness/foo.py:10:5 warning[W123] something", True),
        # Exit code lines
        ("[exit code: 1]", True),
        # Non-test bash outputs should return False
        ("total 128\n-rw-r--r-- 1 user ...", False),
        ("branch main\nyour branch is up to date", False),
        ("", False),
        ("   ", False),
    ])
    def test_detection(self, text, expected):
        assert _bash_is_test_output(text) == expected, (
            f"_bash_is_test_output({text!r}) expected {expected}"
        )

    def test_patterns_are_lowercase(self):
        """All _BASH_TEST_PATTERNS entries should be lowercase (detection uses lower())."""
        for pat in _BASH_TEST_PATTERNS:
            # Unicode symbols may not be lowercase, skip
            if pat.isascii():
                assert pat == pat.lower(), (
                    f"_BASH_TEST_PATTERNS entry {pat!r} should be lowercase"
                )

    def test_empty_string_returns_false(self):
        assert _bash_is_test_output("") is False

    def test_none_like_short_text_returns_false(self):
        # A very short text that has no test patterns
        assert _bash_is_test_output("ok\n") is False


# ---------------------------------------------------------------------------
# Content-aware bash threshold in _prune_conversation_tool_outputs
# ---------------------------------------------------------------------------

class TestBashContentAwarePruning:
    """bash tool outputs that look like test runs get the high-signal threshold."""

    _PYTEST_SUMMARY = "3 passed in 0.12s"

    def _make_bash_output(self, base_content: str, length: int = 1700) -> str:
        """Build a bash output string of the given length that contains test patterns."""
        body = base_content + " " + ("x" * length)
        return body[:length]

    def test_bash_test_output_not_pruned_at_1700_chars(self):
        """bash output with test patterns at 1700 chars should NOT be pruned
        (medium-signal threshold is 1500, but test-output escalates to 2000)."""
        # Build content of exactly 1700 chars that includes pytest summary
        content = self._PYTEST_SUMMARY + " " + ("x" * 1700)
        content = content[:1700]
        conv = _build_two_pair_tool_conversation("bash", content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" not in text_out, (
            "bash test-output at 1700 chars should NOT be pruned (threshold=2000)"
        )

    def test_bash_plain_output_pruned_at_1700_chars(self):
        """bash output WITHOUT test patterns at 1700 chars should be pruned
        (medium-signal threshold is 1500)."""
        # Plain content with no test patterns
        content = "total 128\nsome file listing\n" + ("x" * 1700)
        content = content[:1700]
        conv = _build_two_pair_tool_conversation("bash", content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" in text_out, (
            "bash plain output at 1700 chars should be pruned (threshold=1500)"
        )

    def test_bash_test_output_pruned_above_2000_chars(self):
        """bash test output over 2000 chars should still eventually be pruned."""
        content = self._PYTEST_SUMMARY + " " + ("x" * 2100)
        content = content[:2100]
        conv = _build_two_pair_tool_conversation("bash", content)
        _prune_conversation_tool_outputs(conv, target_chars=0, keep_recent_pairs=1)
        text_out = _extract_first_tool_result_text(conv)
        assert "compacted" in text_out, (
            "bash test-output at 2100 chars should be pruned (threshold=2000)"
        )


# ---------------------------------------------------------------------------
# Content-aware bash threshold in _compact_old_tool_results
# ---------------------------------------------------------------------------

class TestBashContentAwareCompaction:
    """bash outputs with test patterns use high-signal thresholds in compaction.

    _compact_old_tool_results only processes the OLDEST pairs (all except the
    last _COMPACT_KEEP_RECENT=3).  We build 4 pairs so pair[0] is processed.
    """

    _NUM_PAIRS = _COMPACT_KEEP_RECENT + 1  # 4 pairs → first pair gets compacted

    def _build_bash_conv(self, content: str) -> list[dict]:
        """Build a conversation where pair[0] is a bash call with *content*."""
        # Pair 0: bash with the test content
        pair0_a, pair0_u = _make_pair("t0", "bash", content)
        # Remaining pairs: tiny generic tool calls to push pair0 into "old" zone
        rest = _build_conversation(self._NUM_PAIRS - 1, result_text="ok", tool_name="bash")
        return [pair0_a, pair0_u] + rest

    def test_bash_test_output_uses_high_signal_min_len(self):
        """bash output with test patterns is not compacted below 2000 chars."""
        content = "3 passed in 0.12s " + ("x" * 1900)
        content = content[:1900]  # below 2000
        conv = self._build_bash_conv(content)
        _compact_old_tool_results(conv)
        # pair0 is the first tool_result block
        first_text = conv[1]["content"][0]["content"][0]["text"]
        assert "compacted" not in first_text, (
            "bash test-output at 1900 chars should NOT be compacted (min_len=2000)"
        )

    def test_bash_plain_output_uses_medium_signal_min_len(self):
        """bash output without test patterns is compacted above 1500 chars."""
        content = "some plain bash output " + ("x" * 1600)
        content = content[:1600]  # above 1500
        conv = self._build_bash_conv(content)
        _compact_old_tool_results(conv)
        first_text = conv[1]["content"][0]["content"][0]["text"]
        assert "compacted" in first_text, (
            "bash plain output at 1600 chars should be compacted (min_len=1500)"
        )

    def test_bash_test_output_compacted_above_2000_chars(self):
        """bash test output above 2000 chars is still compacted."""
        content = "3 passed in 0.12s " + ("x" * 2200)
        content = content[:2200]
        conv = self._build_bash_conv(content)
        _compact_old_tool_results(conv)
        first_text = conv[1]["content"][0]["content"][0]["text"]
        assert "compacted" in first_text, (
            "bash test-output at 2200 chars should be compacted (min_len=2000)"
        )


# ---------------------------------------------------------------------------
# Tests for _CachedToolRegistry batch_read offset/limit cache behaviour
# ---------------------------------------------------------------------------


def _make_ok_result(text: str = "content") -> ToolResult:
    """Helper: a successful ToolResult with body text."""
    return ToolResult(output=text)


def _make_mock_registry(return_text: str = "content") -> MagicMock:
    inner = MagicMock()
    inner._tools = {}  # noqa: SLF001
    inner.to_api_schema.return_value = []
    inner.execute = AsyncMock(return_value=_make_ok_result(return_text))
    return inner


class TestCachedToolRegistryBatchReadCache:
    """The batch_read cache must distinguish calls with different offset/limit
    pairs so that reading a second slice of the same file works correctly."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_same_offset_limit_cached(self):
        """Requesting the same (path, offset, limit) twice returns the cache
        hint and does not call the inner execute a second time."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        params = {"paths": ["foo.py"], "offset": 1, "limit": 100}

        # First call — should execute normally.
        r1 = self._run(reg.execute("batch_read", config, params))
        assert not r1.is_error
        assert inner.execute.call_count == 1

        # Second call with identical params — cache hit; no new execute call.
        r2 = self._run(reg.execute("batch_read", config, params))
        assert inner.execute.call_count == 1
        # The cache-hint result must contain the "already read" annotation.
        text = r2.output
        assert "already read" in text

    def test_different_offset_not_cached(self):
        """Requesting the same file with a different offset must NOT be a
        cache hit — it should call the inner execute again."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        # First call: offset 1, limit 100.
        self._run(reg.execute("batch_read", config, {"paths": ["foo.py"], "offset": 1, "limit": 100}))
        assert inner.execute.call_count == 1

        # Second call: different offset — must NOT be a cache hit.
        r2 = self._run(reg.execute("batch_read", config, {"paths": ["foo.py"], "offset": 101, "limit": 100}))
        assert not r2.is_error
        assert inner.execute.call_count == 2

    def test_different_limit_not_cached(self):
        """Requesting the same file with a different limit must NOT be a
        cache hit."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        self._run(reg.execute("batch_read", config, {"paths": ["foo.py"], "offset": 1, "limit": 100}))
        assert inner.execute.call_count == 1

        # Same offset, different limit — must NOT be a cache hit.
        r2 = self._run(reg.execute("batch_read", config, {"paths": ["foo.py"], "offset": 1, "limit": 200}))
        assert not r2.is_error
        assert inner.execute.call_count == 2

    def test_write_invalidates_read_cache(self):
        """Writing to a file must clear its entry from the read cache so
        the next batch_read fetches fresh content."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        # First read — populates cache.
        self._run(reg.execute("batch_read", config, {"paths": ["bar.py"], "offset": 1, "limit": 100}))
        assert inner.execute.call_count == 1

        # Simulate a write to bar.py (write_file invalidates path).
        inner.execute.return_value = _make_ok_result("written")
        self._run(reg.execute("write_file", config, {"path": "bar.py", "content": "new"}))
        assert inner.execute.call_count == 2

        # Read again with same params — cache should be invalidated.
        inner.execute.return_value = _make_ok_result("fresh content")
        r3 = self._run(reg.execute("batch_read", config, {"paths": ["bar.py"], "offset": 1, "limit": 100}))
        assert not r3.is_error
        assert inner.execute.call_count == 3

    def test_default_offset_limit_cache_key(self):
        """batch_read without explicit offset/limit should use (1, 2000) as
        the cache key so repeated default calls are cached."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        # Call without offset/limit (uses defaults).
        self._run(reg.execute("batch_read", config, {"paths": ["baz.py"]}))
        assert inner.execute.call_count == 1

        # Second call with same defaults — should be a cache hit.
        r2 = self._run(reg.execute("batch_read", config, {"paths": ["baz.py"]}))
        assert inner.execute.call_count == 1
        text = r2.output
        assert "already read" in text

    def test_multiple_files_partial_cache(self):
        """When some paths are cached and others are not, the fetch should
        only go to the inner registry for the uncached paths."""
        inner = _make_mock_registry()
        reg = _CachedToolRegistry(inner)
        config = MagicMock()

        # Pre-warm cache for "a.py" only.
        self._run(reg.execute("batch_read", config, {"paths": ["a.py"], "offset": 1, "limit": 100}))
        assert inner.execute.call_count == 1

        # Request both "a.py" (cached) and "b.py" (not cached).
        r2 = self._run(reg.execute("batch_read", config, {"paths": ["a.py", "b.py"], "offset": 1, "limit": 100}))
        # inner.execute should be called once more (for b.py only).
        assert inner.execute.call_count == 2
        # The merged result should not be an error.
        assert not r2.is_error
