"""Tests for harness/core/llm.py pure functions.

Covers pruning, compaction, and stub-generation without making any API calls.
All tested functions are importable directly from harness.core.llm.
"""
from __future__ import annotations

import copy
import pytest

from harness.core.llm import (
    _COMPACT_KEEP_RECENT,
    _COMPACT_MIN_TEXT_LEN,
    _COMPACT_PREVIEW_CHARS,
    _COMPACT_MIN_TURNS,
    _CONV_PRUNE_KEEP_RECENT_PAIRS,
    _HIGH_SIGNAL_PATTERNS,
    _HIGH_SIGNAL_TOOLS,
    _LIST_OUTPUT_TOOLS,
    _LOW_SIGNAL_PRUNE_TOOLS,
    _MEDIUM_SIGNAL_TOOLS,
    _PYTEST_SUMMARY_RE,
    _SHORT_PREVIEW_CHARS,
    _SHORT_PREVIEW_TOOLS,
    _compact_old_tool_results,
    _estimate_conversation_chars,
    _make_compact_stub,
    _prune_conversation_tool_outputs,
    _summarise_tool_input,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_use(tool_id: str, name: str = "file_patch", cmd: str = "ls") -> dict:
    # Use a generic tool name (not bash/lint_check/test_runner/python_eval) so
    # tests exercise the *default* compaction threshold (500 chars), not a
    # tool-specific elevated threshold.
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": cmd}}
        ],
    }


def _tool_result(tool_id: str, text: str) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _make_exchanges(n: int, text: str = "x" * 1000) -> list[dict]:
    """Return n tool-use + tool-result pairs."""
    msgs: list[dict] = []
    for i in range(n):
        msgs.append(_tool_use(f"id{i}"))
        msgs.append(_tool_result(f"id{i}", text))
    return msgs


# ---------------------------------------------------------------------------
# _estimate_conversation_chars
# ---------------------------------------------------------------------------

class TestEstimateConversationChars:
    def test_empty_conversation(self):
        assert _estimate_conversation_chars([]) == 0

    def test_string_content(self):
        conv = [{"role": "user", "content": "hello world"}]
        assert _estimate_conversation_chars(conv) == len("hello world")

    def test_text_block_in_list_content(self):
        conv = [{"role": "assistant", "content": [{"type": "text", "text": "response here"}]}]
        assert _estimate_conversation_chars(conv) == len("response here")

    def test_tool_use_block_counts_zero(self):
        # tool_use blocks have no text — they shouldn't add to char count
        conv = [{
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": "bash", "input": {"command": "ls"}}]
        }]
        assert _estimate_conversation_chars(conv) == 0

    def test_tool_result_block(self):
        conv = [_tool_result("x", "file_output")]
        assert _estimate_conversation_chars(conv) == len("file_output")

    def test_multi_turn_accumulates(self):
        conv = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            _tool_result("a", "output"),
        ]
        assert _estimate_conversation_chars(conv) == len("hi") + len("hello") + len("output")

    def test_multiple_text_blocks(self):
        conv = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "part1"},
                {"type": "text", "text": "part2"},
            ],
        }]
        assert _estimate_conversation_chars(conv) == len("part1") + len("part2")

    def test_large_conversation(self):
        conv = _make_exchanges(5, "x" * 1000)
        estimate = _estimate_conversation_chars(conv)
        # Each tool_result adds 1000 chars; tool_use adds 0
        assert estimate == 5000


# ---------------------------------------------------------------------------
# _make_compact_stub
# ---------------------------------------------------------------------------

class TestMakeCompactStub:
    def test_short_text_still_returns_stub_header(self):
        """Even short text gets a stub header."""
        text = "x" * 50
        result = _make_compact_stub("bash", text)
        assert result.startswith("[bash:")
        assert "compacted" in result

    def test_stub_header_contains_original_length(self):
        text = "y" * 300
        result = _make_compact_stub("batch_read", text)
        assert "batch_read" in result
        assert "300" in result

    def test_long_text_includes_preview(self):
        text = "a" * 1000
        result = _make_compact_stub("bash", text)
        # The preview should appear in the output
        assert "preview:" in result

    def test_preview_limited_to_compact_preview_chars(self):
        text = "a" * 2000
        result = _make_compact_stub("bash", text)
        # Find the preview line
        for line in result.splitlines():
            if line.startswith("preview:"):
                preview_text = line[len("preview: "):]
                assert len(preview_text) <= _COMPACT_PREVIEW_CHARS + 5  # small slack
                break
        else:
            pytest.fail("No preview line found in stub")

    def test_signal_lines_extracted_from_long_text(self):
        # The signal line contains 'error:'
        text = "a" * 600 + "\nerror: something went wrong\n" + "b" * 100
        result = _make_compact_stub("bash", text)
        assert "error: something went wrong" in result

    def test_signal_line_with_score(self):
        text = "a" * 600 + "\nscore: 7/10\n" + "b" * 100
        result = _make_compact_stub("bash", text)
        assert "score: 7/10" in result

    def test_signal_line_with_verdict(self):
        text = "a" * 600 + "\nverdict: PASS\n" + "b" * 100
        result = _make_compact_stub("bash", text)
        assert "verdict: PASS" in result

    def test_no_signal_lines_in_short_output(self):
        text = "y" * 100  # below _COMPACT_MIN_TEXT_LEN
        result = _make_compact_stub("bash", text)
        # Short stubs should not have a signal: line
        assert "signal:" not in result

    def test_high_signal_patterns_list(self):
        """All listed HIGH_SIGNAL_PATTERNS should be lowercase strings."""
        for p in _HIGH_SIGNAL_PATTERNS:
            assert isinstance(p, str)
            assert p == p.lower() or "\u2713" in p or "\u2717" in p or "\u2718" in p

    def test_tool_name_in_header(self):
        for name in ["bash", "batch_read", "scratchpad", "custom_tool"]:
            result = _make_compact_stub(name, "x" * 100)
            assert name in result


# ---------------------------------------------------------------------------
# _compact_old_tool_results
# ---------------------------------------------------------------------------

class TestCompactOldToolResults:
    def test_empty_conversation_returns_zero(self):
        assert _compact_old_tool_results([]) == 0

    def test_fewer_than_keep_recent_not_compacted(self):
        # _COMPACT_KEEP_RECENT exchanges should never be compacted
        conv = _make_exchanges(_COMPACT_KEEP_RECENT, "x" * 1000)
        conv_copy = copy.deepcopy(conv)
        n = _compact_old_tool_results(conv_copy)
        assert n == 0

    def test_one_extra_exchange_compacted(self):
        # KEEP_RECENT+1 exchanges → exactly 1 compacted
        conv = _make_exchanges(_COMPACT_KEEP_RECENT + 1, "x" * 1000)
        conv_copy = copy.deepcopy(conv)
        n = _compact_old_tool_results(conv_copy)
        assert n == 1

    def test_short_text_not_compacted(self):
        # Text below _COMPACT_MIN_TEXT_LEN is not worth compacting
        short_text = "x" * (_COMPACT_MIN_TEXT_LEN - 1)
        conv = _make_exchanges(_COMPACT_KEEP_RECENT + 2, short_text)
        conv_copy = copy.deepcopy(conv)
        n = _compact_old_tool_results(conv_copy)
        assert n == 0

    def test_compacted_messages_are_shorter(self):
        long_text = "x" * 1000
        conv = _make_exchanges(_COMPACT_KEEP_RECENT + 2, long_text)
        conv_copy = copy.deepcopy(conv)
        _compact_old_tool_results(conv_copy)
        # First tool result should be compacted (shorter)
        first_user = conv_copy[1]
        block = first_user["content"][0]
        text = block["content"][0]["text"]
        assert len(text) < len(long_text)

    def test_recent_exchanges_preserved_verbatim(self):
        long_text = "z" * 1000
        conv = _make_exchanges(_COMPACT_KEEP_RECENT + 2, long_text)
        conv_copy = copy.deepcopy(conv)
        _compact_old_tool_results(conv_copy)
        # Last _COMPACT_KEEP_RECENT tool results should be untouched
        n = len(conv_copy)
        recent_start_idx = n - _COMPACT_KEEP_RECENT * 2  # 2 msgs per exchange
        for i in range(recent_start_idx, n, 2):  # user messages are at odd positions
            user_msg = conv_copy[i + 1]  # result after tool_use
            block = user_msg["content"][0]
            text = block["content"][0]["text"]
            assert text == long_text, f"Recent exchange at index {i+1} should be untouched"

    def test_returns_count_of_compacted(self):
        n_exchanges = _COMPACT_KEEP_RECENT + 3
        conv = _make_exchanges(n_exchanges, "x" * 1000)
        conv_copy = copy.deepcopy(conv)
        n = _compact_old_tool_results(conv_copy)
        assert n == 3  # n_exchanges - KEEP_RECENT

    def test_conversation_length_unchanged(self):
        # Compaction modifies content but doesn't remove messages
        n = 8
        conv = _make_exchanges(n, "x" * 1000)
        conv_copy = copy.deepcopy(conv)
        _compact_old_tool_results(conv_copy)
        assert len(conv_copy) == len(conv)


# ---------------------------------------------------------------------------
# _prune_conversation_tool_outputs
# ---------------------------------------------------------------------------

class TestPruneConversationToolOutputs:
    def test_empty_returns_empty(self):
        result, n_pruned, saved = _prune_conversation_tool_outputs([], 10000, 3)
        assert result == []
        assert n_pruned == 0
        assert saved == 0

    def test_conversation_without_tools_unchanged(self):
        conv = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result, n_pruned, saved = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=10000, keep_recent_pairs=3
        )
        assert n_pruned == 0
        assert saved == 0

    def test_returns_tuple_of_three(self):
        conv = _make_exchanges(5, "x" * 1000)
        out = _prune_conversation_tool_outputs(copy.deepcopy(conv), 100, 3)
        assert isinstance(out, tuple)
        assert len(out) == 3

    def test_pruned_messages_shorter(self):
        # Uses tool name "file_patch" (default in _make_exchanges); threshold for
        # default/non-classified tools is 500, so text must exceed 500 chars.
        long_text = "x" * 1000
        conv = _make_exchanges(10, long_text)
        result, n_pruned, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=100, keep_recent_pairs=3
        )
        assert n_pruned > 0
        # Earlier messages should be shorter
        first_user = result[1]
        block = first_user["content"][0]
        text = block["content"][0]["text"]
        assert len(text) < len(long_text)

    def test_recent_pairs_preserved(self):
        # Must be > 500 chars so default threshold doesn't protect it.
        long_text = "z" * 1000
        n = 10
        keep = 3
        conv = _make_exchanges(n, long_text)
        result, _, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=keep
        )
        # Last `keep` tool results should be untouched
        # They appear at positions: (n-keep)*2+1, (n-keep)*2+3, ...
        start_idx = (n - keep) * 2 + 1  # position of first recent user msg
        for i in range(start_idx, len(result), 2):
            if i < len(result):
                msg = result[i]
                if msg["role"] == "user":
                    block = msg["content"][0]
                    if block.get("type") == "tool_result":
                        text = block["content"][0]["text"]
                        assert text == long_text, f"Recent pair at {i} should be preserved"

    def test_saved_chars_positive_when_pruned(self):
        conv = _make_exchanges(10, "x" * 1000)
        _, n_pruned, saved = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=100, keep_recent_pairs=3
        )
        if n_pruned > 0:
            assert saved > 0

    def test_message_count_unchanged(self):
        conv = _make_exchanges(8, "x" * 1000)
        result, _, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=2
        )
        assert len(result) == len(conv)

    def test_default_keep_recent_constant(self):
        # Verify the module constant used in real code
        assert _CONV_PRUNE_KEEP_RECENT_PAIRS > 0
        assert isinstance(_CONV_PRUNE_KEEP_RECENT_PAIRS, int)

    def test_stub_uses_make_compact_format(self):
        """After pruning, the stub should be in _make_compact_stub format,
        not the old '[pruned — N chars ...]' primitive format."""
        long_text = "summary: all good\n" + "x" * 1000
        conv = _make_exchanges(5, long_text)
        result, n_pruned, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=1
        )
        assert n_pruned > 0
        # Find the first pruned block
        found_stub = False
        for msg in result:
            if msg["role"] != "user":
                continue
            for block in msg.get("content", []):
                if block.get("type") != "tool_result":
                    continue
                for sub in block.get("content", []):
                    txt = sub.get("text", "")
                    if txt != long_text:
                        # This is a stub — check it uses compact format
                        assert "[pruned" in txt or "chars" in txt or "preview" in txt.lower()
                        found_stub = True
                        break
        assert found_stub, "Expected at least one pruned message"

    def test_low_signal_tool_pruned_at_small_size(self):
        """Low-signal tools (e.g. grep_search) should be pruned at > 200 chars,
        while regular tools (bash) are only pruned at > 500 chars."""
        # Build a conversation where the assistant uses a low-signal tool.
        # The low-signal block should get pruned; use 1000 chars so the compact
        # stub (header + up to 300-char preview) is guaranteed shorter.
        low_signal_text = "x" * 1000  # 1000 > 200 threshold for low-signal tools

        conv = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "lid0", "name": "grep_search", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "lid0",
                        "content": [{"type": "text", "text": low_signal_text}],
                    }
                ],
            },
            # Add extra to ensure we have more than keep_recent_pairs exchanges.
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "lid1", "name": "grep_search", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "lid1",
                        "content": [{"type": "text", "text": low_signal_text}],
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "lid2", "name": "grep_search", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "lid2",
                        "content": [{"type": "text", "text": low_signal_text}],
                    }
                ],
            },
        ]
        # target_chars=0 forces pruning; keep_recent_pairs=1 protects only last pair
        result, n_pruned, saved = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=1
        )
        assert n_pruned > 0, "grep_search (low-signal) should have been pruned"
        assert saved > 0

    def test_high_signal_tool_not_pruned_below_threshold(self):
        """Medium-signal tools like bash should NOT be pruned when text <= 1500 chars."""
        # 400 chars < 1500 threshold for bash (medium-signal tool)
        text_400 = "a" * 400
        conv: list[dict] = []
        # Build 6 exchanges so we have enough to trigger pruning of old ones
        for i in range(6):
            uid = f"hid{i}"
            conv.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": uid, "name": "bash", "input": {}}],
            })
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "content": [{"type": "text", "text": text_400}],
                }],
            })
        result, n_pruned, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=1
        )
        # Nothing should be pruned because 400 <= 1500 threshold for bash
        assert n_pruned == 0, "bash output <= 1500 chars should NOT be pruned"

    def test_low_signal_tool_set_contains_expected_tools(self):
        """Verify _LOW_SIGNAL_PRUNE_TOOLS has expected entries."""
        assert "grep_search" in _LOW_SIGNAL_PRUNE_TOOLS
        assert "glob_search" in _LOW_SIGNAL_PRUNE_TOOLS
        assert "list_directory" in _LOW_SIGNAL_PRUNE_TOOLS
        assert "tree" in _LOW_SIGNAL_PRUNE_TOOLS
        assert "tool_discovery" in _LOW_SIGNAL_PRUNE_TOOLS
        # High-signal tools must NOT be in the set
        assert "bash" not in _LOW_SIGNAL_PRUNE_TOOLS
        assert "test_runner" not in _LOW_SIGNAL_PRUNE_TOOLS
        assert "batch_read" not in _LOW_SIGNAL_PRUNE_TOOLS
        assert "python_eval" not in _LOW_SIGNAL_PRUNE_TOOLS

    def test_high_signal_tools_set_contains_expected_entries(self):
        """_HIGH_SIGNAL_TOOLS should include critical diagnostic tools."""
        assert "test_runner" in _HIGH_SIGNAL_TOOLS
        assert "python_eval" in _HIGH_SIGNAL_TOOLS
        assert "lint_check" in _HIGH_SIGNAL_TOOLS
        # bash is in _MEDIUM_SIGNAL_TOOLS, not _HIGH_SIGNAL_TOOLS
        assert "bash" not in _HIGH_SIGNAL_TOOLS
        # Low-signal tools must NOT be in the set
        assert "grep_search" not in _HIGH_SIGNAL_TOOLS
        assert "glob_search" not in _HIGH_SIGNAL_TOOLS

    def test_high_signal_tool_preserved_at_moderate_size(self):
        """test_runner output below 2000 chars should NOT be compacted."""
        text_1500 = "PASSED 1500 tests in 7.3s " + "x" * 1470
        conv: list[dict] = []
        for i in range(6):
            uid = f"tsid{i}"
            conv.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": uid, "name": "test_runner", "input": {}}],
            })
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "content": [{"type": "text", "text": text_1500}],
                }],
            })
        result, n_pruned, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=1
        )
        # 1500 chars < 2000 threshold for test_runner — should not be pruned
        assert n_pruned == 0, "test_runner output < 2000 chars should NOT be pruned"

    def test_high_signal_tool_compacted_when_enormous(self):
        """test_runner output > 2000 chars SHOULD be compacted (must save space)."""
        text_huge = "PASSED " + "x" * 3000
        conv: list[dict] = []
        for i in range(6):
            uid = f"tsid{i}"
            conv.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": uid, "name": "test_runner", "input": {}}],
            })
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "content": [{"type": "text", "text": text_huge}],
                }],
            })
        result, n_pruned, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=0, keep_recent_pairs=1
        )
        # 3007 chars > 2000 threshold — old turns should be pruned
        assert n_pruned > 0, "test_runner output > 2000 chars should be pruned in old turns"


# ---------------------------------------------------------------------------
# _summarise_tool_input
# ---------------------------------------------------------------------------

class TestSummariseToolInput:
    def test_bash_command(self):
        result = _summarise_tool_input("bash", {"command": "ls -la"})
        assert result == "($ ls -la)"

    def test_bash_empty_command(self):
        result = _summarise_tool_input("bash", {})
        # Should handle missing command gracefully
        assert isinstance(result, str)

    def test_batch_read_paths(self):
        result = _summarise_tool_input("batch_read", {"paths": ["a.py", "b.py"]})
        assert "paths" in result
        assert "a.py" in result

    def test_scratchpad_note(self):
        result = _summarise_tool_input("scratchpad", {"note": "my note"})
        assert "note" in result
        assert "my note" in result

    def test_unknown_tool_returns_first_param(self):
        result = _summarise_tool_input("unknown_tool", {"foo": "bar", "baz": "qux"})
        assert isinstance(result, str)
        # Should return something non-empty for any tool
        assert len(result) > 0

    def test_returns_string(self):
        """All paths return a str."""
        for tool, params in [
            ("bash", {"command": "echo hi"}),
            ("batch_read", {"paths": []}),
            ("edit_file", {"path": "x.py", "old_str": "a", "new_str": "b"}),
            ("weird_tool", {}),
        ]:
            assert isinstance(_summarise_tool_input(tool, params), str)

    def test_bash_long_command_truncated(self):
        long_cmd = "echo " + "a" * 500
        result = _summarise_tool_input("bash", {"command": long_cmd})
        # Should not balloon the output
        assert len(result) < len(long_cmd) + 50  # some slack for wrapping


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------

class TestLLMConstants:
    def test_compact_min_turns_positive(self):
        assert _COMPACT_MIN_TURNS > 0

    def test_compact_keep_recent_positive(self):
        assert _COMPACT_KEEP_RECENT > 0

    def test_compact_min_text_len_positive(self):
        assert _COMPACT_MIN_TEXT_LEN > 0

    def test_compact_preview_chars_positive(self):
        assert _COMPACT_PREVIEW_CHARS > 0

    def test_conv_prune_keep_recent_pairs_positive(self):
        assert _CONV_PRUNE_KEEP_RECENT_PAIRS > 0

    def test_compact_keep_recent_less_than_min_turns(self):
        # keep_recent should be smaller than min_turns so compaction makes sense
        assert _COMPACT_KEEP_RECENT < _COMPACT_MIN_TURNS

    def test_high_signal_patterns_non_empty(self):
        assert len(_HIGH_SIGNAL_PATTERNS) > 0

    def test_high_signal_patterns_contains_error_and_score(self):
        # These are critical patterns for preserving diagnostic info
        assert "error" in _HIGH_SIGNAL_PATTERNS
        assert "score" in _HIGH_SIGNAL_PATTERNS

    def test_high_signal_patterns_has_test_outcome_patterns(self):
        # Test output uses 'passed' and 'failed' — both must be detected
        assert "passed" in _HIGH_SIGNAL_PATTERNS
        assert "failed" in _HIGH_SIGNAL_PATTERNS

    def test_high_signal_patterns_has_exception_patterns(self):
        assert "exception" in _HIGH_SIGNAL_PATTERNS
        assert "traceback" in _HIGH_SIGNAL_PATTERNS

    def test_high_signal_patterns_avoids_over_broad_pass(self):
        # 'pass' alone would match 'password', 'bypass', etc.
        # We prefer 'passed' instead.
        assert "pass" not in _HIGH_SIGNAL_PATTERNS
        assert "passed" in _HIGH_SIGNAL_PATTERNS


# ---------------------------------------------------------------------------
# Integration-style: end-to-end pruning on realistic conversation
# ---------------------------------------------------------------------------

class TestPruningIntegration:
    def test_prune_then_compact_reduces_total_chars(self):
        """Running both pruning stages should reduce total chars."""
        # Build a long conversation
        conv = _make_exchanges(15, "x" * 800)
        original_chars = _estimate_conversation_chars(conv)

        # First compact old results
        conv_copy = copy.deepcopy(conv)
        _compact_old_tool_results(conv_copy)
        after_compact = _estimate_conversation_chars(conv_copy)

        # Then prune further if needed
        result, _, _ = _prune_conversation_tool_outputs(
            conv_copy, target_chars=1000, keep_recent_pairs=3
        )
        after_prune = _estimate_conversation_chars(result)

        assert after_compact <= original_chars
        assert after_prune <= after_compact

    def test_mixed_conversation_only_tool_results_pruned(self):
        """Regular assistant text messages are never pruned."""
        long_assistant_text = "a" * 2000
        conv = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": [{"type": "text", "text": long_assistant_text}]},
        ]
        # Add some tool exchanges on top
        conv.extend(_make_exchanges(5, "x" * 800))
        result, _, _ = _prune_conversation_tool_outputs(
            copy.deepcopy(conv), target_chars=100, keep_recent_pairs=1
        )
        # The assistant text message should be untouched
        assert result[1]["content"][0]["text"] == long_assistant_text

    def test_high_signal_error_preserved_in_compact_stub(self):
        """When a tool result has an error line, it appears in the compact stub."""
        error_text = "x" * 600 + "\nTraceback (most recent call last):\n  File test.py line 10\nAssertionError: count mismatch"
        stub = _make_compact_stub("bash", error_text)
        assert "traceback" in stub.lower() or "assertionerror" in stub.lower() or "error" in stub.lower()


# ---------------------------------------------------------------------------
# Tool-type-aware compaction thresholds
# ---------------------------------------------------------------------------

class TestToolTypeAwareCompaction:
    """Test that compaction thresholds differ by tool category."""

    def _make_single_exchange(
        self, tool_name: str, text: str, uid: str = "t1"
    ) -> list[dict]:
        """Minimal conversation: 1 tool-use + 1 tool-result (no pruning guard)."""
        # Build enough exchanges that the turn is not in the 'recent' window
        conv: list[dict] = []
        for i in range(6):
            eid = f"e{i}"
            conv.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": eid, "name": tool_name, "input": {}}],
            })
            conv.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": eid,
                    "content": [{"type": "text", "text": text}],
                }],
            })
        return conv

    def test_medium_signal_tools_membership(self):
        """bash and code-analysis tools are in _MEDIUM_SIGNAL_TOOLS; lint_check is in _HIGH_SIGNAL_TOOLS."""
        assert "bash" in _MEDIUM_SIGNAL_TOOLS
        # Code-reading tools: preserve function bodies (up to 1500 chars) across turns
        assert "symbol_extractor" in _MEDIUM_SIGNAL_TOOLS
        assert "code_analysis" in _MEDIUM_SIGNAL_TOOLS
        assert "cross_reference" in _MEDIUM_SIGNAL_TOOLS
        assert "call_graph" in _MEDIUM_SIGNAL_TOOLS
        assert "data_flow" in _MEDIUM_SIGNAL_TOOLS
        assert "dependency_analyzer" in _MEDIUM_SIGNAL_TOOLS
        # lint_check is already high-signal, not medium
        assert "lint_check" not in _MEDIUM_SIGNAL_TOOLS
        assert "lint_check" in _HIGH_SIGNAL_TOOLS
        # Generic/navigational tools must NOT be in the set
        assert "batch_read" not in _MEDIUM_SIGNAL_TOOLS
        assert "grep_search" not in _MEDIUM_SIGNAL_TOOLS

    def test_bash_not_compacted_below_1500_chars(self):
        """bash outputs under 1500 chars should NOT be compacted."""
        text_1000 = "bash output " * 80  # ~960 chars — well under 1500
        conv = self._make_single_exchange("bash", text_1000)
        original_text = text_1000
        _compact_old_tool_results(conv)
        # Find the first result block
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    assert result_text == original_text, (
                        f"bash output of {len(text_1000)} chars should NOT be compacted "
                        f"(threshold is 1500); got: {result_text[:80]}"
                    )

    def test_bash_compacted_above_1500_chars(self):
        """bash outputs over 1500 chars should be compacted."""
        text_1600 = "bash output line\n" * 100  # ~1700 chars — above 1500
        conv = self._make_single_exchange("bash", text_1600)
        _compact_old_tool_results(conv)
        found_compacted = False
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    if "[bash:" in result_text and "compacted" in result_text:
                        found_compacted = True
        assert found_compacted, (
            f"bash output of {len(text_1600)} chars should be compacted at 1500 threshold"
        )

    def test_lint_check_compacted_above_2000_chars(self):
        """lint_check is high-signal (threshold 2000), so it compacts above 2000 chars."""
        text = "W001: lint issue on line 1\n" * 80  # ~2160 chars
        conv = self._make_single_exchange("lint_check", text)
        _compact_old_tool_results(conv)
        found_compacted = False
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    if "[lint_check:" in result_text and "compacted" in result_text:
                        found_compacted = True
        assert found_compacted, (
            f"lint_check output of {len(text)} chars should be compacted "
            "(threshold is 2000 for high-signal tools)"
        )

    def test_default_tool_compacted_above_500_chars(self):
        """Generic tools with no special category compact at 500 chars."""
        text = "generic tool output\n" * 30  # ~630 chars — above 500
        conv = self._make_single_exchange("batch_read", text)
        _compact_old_tool_results(conv)
        found_compacted = False
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    if "[batch_read:" in result_text and "compacted" in result_text:
                        found_compacted = True
        assert found_compacted, "batch_read output above 500 chars should be compacted"

    def test_bash_stub_has_more_signal_lines_than_default(self):
        """bash _make_compact_stub with max_signal_lines=12 captures more errors."""
        # Create text with 12 distinct error lines
        errors = [f"NameError: name 'var{i}' is not defined" for i in range(12)]
        text = ("x" * 100 + "\n") + "\n".join(errors)
        stub_default = _make_compact_stub("bash", text, max_signal_lines=8)
        stub_medium = _make_compact_stub("bash", text, max_signal_lines=12)
        # The medium stub should capture more signal lines
        default_signals = stub_default.count(" | ")
        medium_signals = stub_medium.count(" | ")
        assert medium_signals >= default_signals, (
            "Higher max_signal_lines should capture at least as many signal lines"
        )
        assert medium_signals > 0, "Should have found some NameError signal lines"

    @pytest.mark.parametrize("tool_name", [
        "symbol_extractor",
        "code_analysis",
        "cross_reference",
        "call_graph",
        "data_flow",
        "dependency_analyzer",
    ])
    def test_code_analysis_tools_use_medium_threshold(self, tool_name: str):
        """Code-analysis tools should NOT be compacted below 1500 chars (medium threshold)."""
        # Build a function body of ~1000 chars — above the 500-char default but below
        # the 1500-char medium threshold.  A default-classified tool would compact this;
        # a medium-signal tool should leave it intact.
        func_body = "def my_func(x, y):\n    return x + y\n" * 25  # ~950 chars
        conv = self._make_single_exchange(tool_name, func_body)
        _compact_old_tool_results(conv)
        # Check that the output was NOT compacted (still intact)
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    assert f"[{tool_name}:" not in result_text or "compacted" not in result_text, (
                        f"{tool_name} output of {len(func_body)} chars should NOT be compacted "
                        f"(medium threshold is 1500); got stub: {result_text[:120]}"
                    )

    @pytest.mark.parametrize("tool_name", [
        "symbol_extractor",
        "code_analysis",
        "cross_reference",
        "call_graph",
        "data_flow",
        "dependency_analyzer",
    ])
    def test_code_analysis_tools_compacted_above_1500_chars(self, tool_name: str):
        """Code-analysis tools should be compacted above 1500 chars (medium threshold)."""
        func_body = "def my_func(x, y):\n    return x + y\n" * 50  # ~1900 chars
        conv = self._make_single_exchange(tool_name, func_body)
        _compact_old_tool_results(conv)
        found_compacted = False
        for msg in conv:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block["content"][0]["text"]
                    if f"[{tool_name}:" in result_text and "compacted" in result_text:
                        found_compacted = True
        assert found_compacted, (
            f"{tool_name} output of {len(func_body)} chars should be compacted "
            "(medium threshold is 1500 chars)"
        )


# ---------------------------------------------------------------------------
# New Python error patterns in _HIGH_SIGNAL_PATTERNS
# ---------------------------------------------------------------------------

class TestNewHighSignalPatterns:
    """Test that additional Python error types are recognised as high-signal."""

    @pytest.mark.parametrize("error_kw", [
        "nameerror",
        "attributeerror",
        "syntaxerror",
        "importerror",
        "modulenotfounderror",
        "keyerror",
        "indexerror",
        "runtimeerror",
        "filenotfounderror",
    ])
    def test_python_error_in_high_signal_patterns(self, error_kw: str):
        assert error_kw in _HIGH_SIGNAL_PATTERNS, (
            f"'{error_kw}' should be in _HIGH_SIGNAL_PATTERNS so it is preserved "
            "in bash/test_runner compact stubs"
        )

    @pytest.mark.parametrize("error_line,expected_preserved", [
        ("NameError: name 'foo' is not defined", True),
        ("AttributeError: 'NoneType' object has no attribute 'bar'", True),
        ("SyntaxError: invalid syntax (module.py, line 42)", True),
        ("ImportError: cannot import name 'X' from 'module'", True),
        ("ModuleNotFoundError: No module named 'missing_pkg'", True),
    ])
    def test_python_error_lines_appear_in_stub(self, error_line: str, expected_preserved: bool):
        """Lines with Python error keywords should appear in compact stubs."""
        text = "x" * 200 + "\n" + error_line + "\n" + "y" * 200
        stub = _make_compact_stub("bash", text)
        kw_found = any(kw in stub.lower() for kw in [
            "nameerror", "attributeerror", "syntaxerror",
            "importerror", "modulenotfounderror",
        ])
        if expected_preserved:
            assert kw_found, (
                f"Line '{error_line}' should appear in stub signal section; "
                f"got stub: {stub}"
            )


# ---------------------------------------------------------------------------
# _PYTEST_SUMMARY_RE and _make_compact_stub summary extraction
# ---------------------------------------------------------------------------

class TestPytestSummaryRegex:
    """Test the _PYTEST_SUMMARY_RE pattern."""

    def test_matches_passed_only(self):
        assert _PYTEST_SUMMARY_RE.search("42 passed in 1.30s")

    def test_matches_passed_and_failed(self):
        assert _PYTEST_SUMMARY_RE.search("3 failed, 1 passed in 0.12s")

    def test_matches_error(self):
        assert _PYTEST_SUMMARY_RE.search("2 error in 0.05s")

    def test_matches_failed_only(self):
        assert _PYTEST_SUMMARY_RE.search("5 failed")

    def test_does_not_match_plain_numbers(self):
        assert not _PYTEST_SUMMARY_RE.search("Some result: 99")

    def test_does_not_match_warnings_only(self):
        assert not _PYTEST_SUMMARY_RE.search("3 warnings in 0.01s")

    def test_case_insensitive(self):
        assert _PYTEST_SUMMARY_RE.search("10 PASSED in 2s")
        assert _PYTEST_SUMMARY_RE.search("2 FAILED in 0.5s")


class TestMakeCompactStubSummary:
    """Test that _make_compact_stub extracts pytest summary lines."""

    def _long_text(self, extra: str = "") -> str:
        """Build a >500-char output that triggers compaction."""
        base = "some test output\n" * 30
        return base + extra

    def test_test_runner_includes_summary_line(self):
        text = self._long_text("1934 passed, 2 failed in 9.68s")
        stub = _make_compact_stub("test_runner", text)
        assert "summary:" in stub
        assert "1934 passed" in stub

    def test_test_runner_summary_before_signal(self):
        text = self._long_text("10 passed in 0.5s")
        stub = _make_compact_stub("test_runner", text)
        summary_pos = stub.find("summary:")
        signal_pos = stub.find("signal:")
        # summary: should appear before signal: (or signal may be absent)
        if signal_pos != -1:
            assert summary_pos < signal_pos

    def test_bash_tool_includes_summary_when_pytest_output(self):
        text = self._long_text("5 passed, 1 failed in 0.3s")
        stub = _make_compact_stub("bash", text)
        assert "summary:" in stub
        assert "5 passed" in stub

    def test_non_test_tool_no_summary_line(self):
        text = self._long_text("some_file.py\n")
        stub = _make_compact_stub("glob_search", text)
        assert "summary:" not in stub

    def test_test_runner_no_summary_if_no_pytest_line(self):
        # Output without any "N passed/failed" line
        text = ("INFO some log line\n" * 30) + "Execution finished."
        stub = _make_compact_stub("test_runner", text)
        assert "summary:" not in stub

    def test_summary_uses_last_matching_line(self):
        # Multiple matches — should use the last one (final summary is at bottom)
        lines = ["1 passed\n"] * 5 + ["7 passed in 1.0s\n"] * 5
        text = "".join(lines) * 5  # make it long enough
        stub = _make_compact_stub("test_runner", text)
        assert "7 passed" in stub

    def test_summary_truncated_to_max_line_chars(self):
        long_summary = "passed " * 60  # very long line
        text = self._long_text(long_summary + " 1 passed")
        stub = _make_compact_stub("test_runner", text, max_line_chars=50)
        # summary line should be at most 50 chars
        for line in stub.splitlines():
            if line.startswith("summary:"):
                content = line[len("summary:"):].strip()
                assert len(content) <= 50


# ---------------------------------------------------------------------------
# _prune_conversation_tool_outputs uses tool-specific max_signal_lines
# ---------------------------------------------------------------------------

class TestPruneToolOutputsMaxSignalLines:
    """
    Verify _prune_conversation_tool_outputs passes the correct
    max_signal_lines to _make_compact_stub (matching _compact_old_tool_results).
    High-signal tools should preserve 15 signal lines, medium 12, others 8.

    Strategy: build a conversation with 3 identical pairs so that the two
    older pairs fall outside the keep_recent_pairs=1 protection window and
    get compacted.  We then check the stub for the first pair.
    """

    def _build_conv_with_tool_output(
        self, tool_name: str, output_text: str, n_pairs: int = 3
    ) -> list[dict]:
        """
        Construct a conversation with n_pairs tool-use/tool-result pairs.
        The most-recent pair is protected; older pairs are candidates for pruning.
        """
        msgs: list[dict] = []
        for i in range(n_pairs):
            msgs.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"tool_{i}",
                            "name": tool_name,
                            "input": {"path": "x"},
                        }
                    ],
                }
            )
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"tool_{i}",
                            "content": [{"type": "text", "text": output_text}],
                        }
                    ],
                }
            )
        return msgs

    def _signal_line_count(self, stub: str) -> int:
        """Count the number of signal fragments in the stub."""
        for line in stub.splitlines():
            if line.startswith("signal:"):
                return len(line.split(" | "))
        return 0

    def _make_long_signal_text(self, n_lines: int = 25) -> str:
        """
        Build text with many distinct signal-keyword lines and enough filler
        to exceed the 2000-char threshold for high-signal tools.
        """
        lines = [f"FAILED test_function_{i:02d} - AssertionError" for i in range(n_lines)]
        # pad to exceed the 2000-char high-signal threshold
        lines += ["some filler line with extra content to make the text long enough"] * 50
        return "\n".join(lines)

    def _get_first_pair_stub(self, conv: list[dict], tool_name: str) -> str:
        """
        Run _prune_conversation_tool_outputs and return the compacted text
        for the first tool-result pair (index 1 in the conversation).
        keep_recent_pairs=1 protects only the last pair, so older ones get pruned.
        """
        pruned, _, _ = _prune_conversation_tool_outputs(
            conv, target_chars=0, keep_recent_pairs=1
        )
        # The stub is at pruned[1]["content"][0]["content"][0]["text"]
        return pruned[1]["content"][0]["content"][0]["text"]

    def test_high_signal_tool_gets_15_signal_lines(self):
        # test_runner is in _HIGH_SIGNAL_TOOLS — should get up to 15 signal lines
        assert "test_runner" in _HIGH_SIGNAL_TOOLS
        text = self._make_long_signal_text(25)
        conv = self._build_conv_with_tool_output("test_runner", text)
        stub = self._get_first_pair_stub(conv, "test_runner")
        assert "[test_runner:" in stub
        # should preserve more than the default 8 signal lines
        n = self._signal_line_count(stub)
        assert n > 8, f"Expected >8 signal lines for high-signal tool, got {n}"

    def test_medium_signal_tool_gets_12_signal_lines(self):
        # bash (with test-looking output) gets 12 signal lines
        assert "bash" in _MEDIUM_SIGNAL_TOOLS
        text = self._make_long_signal_text(20)
        conv = self._build_conv_with_tool_output("bash", text)
        stub = self._get_first_pair_stub(conv, "bash")
        assert "[bash:" in stub
        n = self._signal_line_count(stub)
        assert n > 8, f"Expected >8 signal lines for medium-signal tool, got {n}"

    def test_low_signal_tool_gets_at_most_5_signal_lines(self):
        # grep_search is in _LOW_SIGNAL_PRUNE_TOOLS — gets aggressive 5 signal lines
        text = self._make_long_signal_text(20)
        conv = self._build_conv_with_tool_output("grep_search", text)
        pruned, _, _ = _prune_conversation_tool_outputs(
            conv, target_chars=0, keep_recent_pairs=1
        )
        stub = pruned[1]["content"][0]["content"][0]["text"]
        n = self._signal_line_count(stub)
        assert n <= 5, f"Expected <=5 signal lines for low-signal tool, got {n}"


# ---------------------------------------------------------------------------
# Tests for _LIST_OUTPUT_TOOLS and _SHORT_PREVIEW_TOOLS compaction strategies
# ---------------------------------------------------------------------------

class TestListOutputToolStubs:
    """glob_search, list_directory, tree, git_log produce pure listings.

    Their compacted stub should show a line count instead of a 300-char preview
    of file paths — a much more useful and compact representation.
    """

    def _make_big_listing(self, n: int = 60, prefix: str = "harness/tools/file") -> str:
        return "\n".join(f"{prefix}{i}.py" for i in range(n))

    def test_glob_search_stub_has_no_preview(self) -> None:
        text = self._make_big_listing(60)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("glob_search", text)
        assert "preview:" not in stub, "glob_search stub should not include a preview"

    def test_glob_search_stub_shows_file_count(self) -> None:
        text = self._make_big_listing(60)
        stub = _make_compact_stub("glob_search", text)
        assert "60 files listed" in stub

    def test_list_directory_stub_has_no_preview(self) -> None:
        text = "\n".join(f"  - item_{i}" for i in range(40))
        # pad to exceed min threshold
        text += "\n" * 40
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("list_directory", text)
        assert "preview:" not in stub

    def test_list_directory_stub_shows_entry_count(self) -> None:
        entries = [f"  - item_{i}" for i in range(30)]
        text = "\n".join(entries) + "\n" * 30
        stub = _make_compact_stub("list_directory", text)
        # non-blank lines = 30
        assert "30 entries listed" in stub

    def test_tree_stub_shows_node_count(self) -> None:
        lines = [f"{'  ' * (i % 4)}└── dir_{i}/" for i in range(40)]
        text = "\n".join(lines)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("tree", text)
        assert "preview:" not in stub
        assert "nodes listed" in stub

    def test_git_log_stub_shows_commit_count(self) -> None:
        lines = [f"abc{i:04d} feat: add feature {i}" for i in range(50)]
        text = "\n".join(lines)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("git_log", text)
        assert "preview:" not in stub
        assert "50 commits listed" in stub

    def test_glob_search_still_shows_signal_lines(self) -> None:
        # If somehow a glob_search output contains an error message, it's captured.
        text = "\n".join(f"harness/tools/file{i}.py" for i in range(60))
        text += "\nERROR: permission denied while scanning"
        stub = _make_compact_stub("glob_search", text)
        assert "error" in stub.lower()

    def test_list_output_tools_set_membership(self) -> None:
        for tool in ("glob_search", "list_directory", "tree", "git_log"):
            assert tool in _LIST_OUTPUT_TOOLS, f"{tool} should be in _LIST_OUTPUT_TOOLS"

    def test_list_output_stub_is_compact(self) -> None:
        """Stub should be much shorter than original 300-char preview + content."""
        text = self._make_big_listing(100)
        stub = _make_compact_stub("glob_search", text)
        # Should not contain more than ~80 chars of path content
        preview_portion = [line for line in stub.splitlines() if line.startswith("preview:")]
        assert not preview_portion, "No preview line expected for list-output tools"


class TestShortPreviewToolStubs:
    """grep_search, git_status, git_diff, etc. use _SHORT_PREVIEW_CHARS (100)
    rather than _COMPACT_PREVIEW_CHARS (300) — shorter but still useful context.
    """

    def _make_grep_output(self, n: int = 60) -> str:
        return "\n".join(
            f"harness/core/llm.py:{10 + i}:    result_{i} = compute(x)"
            for i in range(n)
        )

    def test_grep_search_uses_short_preview(self) -> None:
        text = self._make_grep_output(60)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("grep_search", text)
        assert "preview:" in stub
        # The preview should be at most _SHORT_PREVIEW_CHARS chars long
        preview_line = next(ln for ln in stub.splitlines() if ln.startswith("preview:"))
        preview_content = preview_line[len("preview:"):].strip()
        assert len(preview_content) <= _SHORT_PREVIEW_CHARS + 50  # allow newline break tolerance

    def test_grep_search_preview_shorter_than_full_tool(self) -> None:
        text = self._make_grep_output(60)
        stub_grep = _make_compact_stub("grep_search", text)
        stub_bash = _make_compact_stub("bash", text)
        # grep_search preview should be shorter than bash preview
        def get_preview(stub: str) -> str:
            for line in stub.splitlines():
                if line.startswith("preview:"):
                    return line
            return ""
        grep_preview = get_preview(stub_grep)
        bash_preview = get_preview(stub_bash)
        assert len(grep_preview) <= len(bash_preview)

    def test_short_preview_tools_set_membership(self) -> None:
        for tool in ("grep_search", "git_status", "git_diff"):
            assert tool in _SHORT_PREVIEW_TOOLS, f"{tool} should be in _SHORT_PREVIEW_TOOLS"

    def test_short_preview_chars_smaller_than_full_preview(self) -> None:
        assert _SHORT_PREVIEW_CHARS < _COMPACT_PREVIEW_CHARS

    def test_git_diff_uses_short_preview(self) -> None:
        # A typical large git diff output
        lines = ["diff --git a/foo.py b/foo.py", "index abc..def 100644"]
        lines += ["+" + f" added line {i}" for i in range(40)]
        lines += ["-" + f" removed line {i}" for i in range(40)]
        text = "\n".join(lines)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("git_diff", text)
        assert "preview:" in stub
        preview_line = next(ln for ln in stub.splitlines() if ln.startswith("preview:"))
        # Preview content should fit within short preview + tolerance
        assert len(preview_line) < 200  # Much less than 300+

    def test_tool_discovery_uses_short_preview(self) -> None:
        # tool_discovery output: many tool descriptions
        lines = [f"Tool {i}: description of tool {i} with many words" for i in range(40)]
        text = "\n".join(lines)
        assert len(text) > _COMPACT_MIN_TEXT_LEN
        stub = _make_compact_stub("tool_discovery", text)
        assert "preview:" in stub
        preview_line = next(ln for ln in stub.splitlines() if ln.startswith("preview:"))
        assert len(preview_line) < 200  # Much shorter than 300+
