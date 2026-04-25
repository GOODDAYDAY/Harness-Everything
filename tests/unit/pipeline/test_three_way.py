"""Unit tests for harness/pipeline/three_way.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.pipeline.three_way import (
    ThreeWayResolver,
    ThreeWayResult,
    _is_response_too_short,
)


# ---------------------------------------------------------------------------
# _is_response_too_short tests
# ---------------------------------------------------------------------------


class TestIsResponseTooShort:
    """Tests for _is_response_too_short."""

    def test_empty_string_is_too_short(self) -> None:
        assert _is_response_too_short("", 10) is True

    def test_exact_word_count_not_too_short(self) -> None:
        text = " ".join(["word"] * 10)
        assert _is_response_too_short(text, 10) is False

    def test_one_less_than_min_is_too_short(self) -> None:
        text = " ".join(["word"] * 9)
        assert _is_response_too_short(text, 10) is True

    def test_above_min_not_too_short(self) -> None:
        text = " ".join(["word"] * 100)
        assert _is_response_too_short(text, 10) is False

    def test_zero_min_never_too_short(self) -> None:
        assert _is_response_too_short("", 0) is False

    def test_single_word_above_min_one(self) -> None:
        assert _is_response_too_short("hello", 1) is False

    def test_single_word_below_min_two(self) -> None:
        assert _is_response_too_short("hello", 2) is True


# ---------------------------------------------------------------------------
# ThreeWayResult tests
# ---------------------------------------------------------------------------


class TestThreeWayResult:
    """Tests for the ThreeWayResult dataclass."""

    def test_fields_accessible(self) -> None:
        r = ThreeWayResult(conservative="c", aggressive="a", merged="m")
        assert r.conservative == "c"
        assert r.aggressive == "a"
        assert r.merged == "m"

    def test_equality(self) -> None:
        r1 = ThreeWayResult(conservative="c", aggressive="a", merged="m")
        r2 = ThreeWayResult(conservative="c", aggressive="a", merged="m")
        assert r1 == r2

    def test_inequality_on_merged(self) -> None:
        r1 = ThreeWayResult(conservative="c", aggressive="a", merged="m1")
        r2 = ThreeWayResult(conservative="c", aggressive="a", merged="m2")
        assert r1 != r2


# ---------------------------------------------------------------------------
# ThreeWayResolver.resolve() tests
# ---------------------------------------------------------------------------


def _make_llm_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


class TestThreeWayResolverResolve:
    """Tests for ThreeWayResolver.resolve()."""

    @pytest.mark.asyncio
    async def test_resolve_returns_three_way_result(self) -> None:
        llm = MagicMock()
        long_text = " ".join(["word"] * 80)
        llm.call = AsyncMock(side_effect=[
            _make_llm_response(long_text),   # conservative
            _make_llm_response(long_text),   # aggressive
            _make_llm_response(long_text),   # merge
        ])
        resolver = ThreeWayResolver(llm)
        result = await resolver.resolve(
            "Do the task",
            conservative_system="be conservative",
            aggressive_system="be aggressive",
            merge_system="merge both",
        )
        assert isinstance(result, ThreeWayResult)
        assert result.conservative == long_text
        assert result.aggressive == long_text
        assert result.merged == long_text

    @pytest.mark.asyncio
    async def test_resolve_calls_llm_three_times_normally(self) -> None:
        """With long enough responses no retries occur — expect 3 LLM calls."""
        llm = MagicMock()
        long_text = " ".join(["word"] * 80)
        llm.call = AsyncMock(return_value=_make_llm_response(long_text))
        resolver = ThreeWayResolver(llm)
        await resolver.resolve(
            "task",
            conservative_system="sys",
            aggressive_system="sys",
            merge_system="sys",
        )
        # 2 parallel (conservative + aggressive) + 1 merge = 3 total
        assert llm.call.call_count == 3

    @pytest.mark.asyncio
    async def test_resolve_retries_on_short_conservative_response(self) -> None:
        """A short conservative response triggers a retry."""
        llm = MagicMock()
        short_text = "too short"
        long_text = " ".join(["word"] * 80)
        # conservative short → retry; aggressive long (no retry); merge long
        llm.call = AsyncMock(side_effect=[
            _make_llm_response(short_text),   # conservative first try
            _make_llm_response(long_text),    # aggressive first try
            _make_llm_response(long_text),    # conservative retry
            _make_llm_response(long_text),    # merge
        ])
        resolver = ThreeWayResolver(llm)
        result = await resolver.resolve(
            "task",
            conservative_system="sys",
            aggressive_system="sys",
            merge_system="sys",
        )
        # 4 calls total: 2 parallel + 1 retry + 1 merge
        assert llm.call.call_count == 4
        assert result.conservative == long_text  # retry result used

    @pytest.mark.asyncio
    async def test_resolve_merge_uses_both_responses(self) -> None:
        """The merge call receives both conservative and aggressive text."""
        llm = MagicMock()
        long_text = " ".join(["word"] * 80)
        merge_text = " ".join(["merged"] * 80)

        call_messages: list[list[dict]] = []

        async def capture_call(messages: list[dict], *, system: str) -> MagicMock:
            call_messages.append(messages)
            return _make_llm_response(long_text if len(call_messages) < 3 else merge_text)

        llm.call = capture_call
        resolver = ThreeWayResolver(llm)
        result = await resolver.resolve(
            "task",
            conservative_system="cons_sys",
            aggressive_system="aggr_sys",
            merge_system="merge_sys",
        )
        # Third call is merge — its message content should contain both proposals
        merge_msg = call_messages[2][0]["content"]
        assert "Conservative Proposal" in merge_msg
        assert "Aggressive Proposal" in merge_msg
        assert result.merged == merge_text
