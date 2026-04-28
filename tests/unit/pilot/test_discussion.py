"""Tests for Discussion — multi-turn LLM conversation and proposal tracking."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from harness.pilot.discussion import Discussion


def _make_llm(response_text: str = "OK") -> MagicMock:
    """Create a mock LLM that returns a fixed response."""
    llm = MagicMock()
    resp = MagicMock()
    resp.text = response_text
    llm.call = AsyncMock(return_value=resp)
    return llm


class TestDiscussionBasics:
    """Core discussion functionality."""

    @pytest.mark.asyncio
    async def test_US06_respond_returns_llm_text(self):
        """US-06: Respond returns LLM-generated answer."""
        llm = _make_llm("Here is my answer about the proposal.")
        disc = Discussion("proposal text", "diagnostic data", llm)
        result = await disc.respond("What did you find?")
        assert result == "Here is my answer about the proposal."

    @pytest.mark.asyncio
    async def test_US06_multi_turn_context(self):
        """US-06: Multi-turn conversation maintains full history."""
        llm = _make_llm("response")
        disc = Discussion("proposal", "context", llm)

        await disc.respond("question 1")
        await disc.respond("question 2")
        await disc.respond("question 3")

        # LLM should be called with all 6 messages (3 user + 3 assistant)
        last_call = llm.call.call_args
        messages = last_call[0][0]
        assert len(messages) == 6
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_current_proposal_initially_matches_original(self):
        """Current proposal equals original before any modifications."""
        disc = Discussion("original plan", "data", _make_llm())
        assert disc.current_proposal == "original plan"


class TestProposalModification:
    """US-07: Operator can modify the proposal through discussion."""

    @pytest.mark.asyncio
    async def test_US07_proposal_updated_when_marker_present(self):
        """US-07: Proposal updates when LLM response contains '### 建议改进'."""
        revised = (
            "修订后的方案：\n"
            "### 建议改进\n"
            "- 只修复错误处理\n"
            "- 跳过模块 X"
        )
        llm = _make_llm(revised)
        disc = Discussion("original plan", "data", llm)

        await disc.respond("skip module X")
        assert disc.current_proposal == revised

    @pytest.mark.asyncio
    async def test_US07_proposal_not_updated_without_marker(self):
        """Proposal stays unchanged when LLM response is a plain answer."""
        llm = _make_llm("Sure, I understand your concern about module X.")
        disc = Discussion("original plan", "data", llm)

        await disc.respond("what about module X?")
        assert disc.current_proposal == "original plan"

    @pytest.mark.asyncio
    async def test_US07_multiple_modifications_tracked(self):
        """Multiple rounds of modification update current_proposal each time."""
        llm = MagicMock()
        call_count = 0

        async def _call(messages, system=None):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count == 1:
                resp.text = "### 建议改进\n- Revised v1"
            elif call_count == 2:
                resp.text = "### 建议改进\n- Revised v2"
            else:
                resp.text = "No changes."
            return resp

        llm.call = _call
        disc = Discussion("original", "data", llm)

        await disc.respond("change A")
        assert "v1" in disc.current_proposal

        await disc.respond("change B")
        assert "v2" in disc.current_proposal


class TestContextTruncation:
    """Diagnostic context truncation for large data sets."""

    @pytest.mark.asyncio
    async def test_large_context_truncated(self):
        """Diagnostic context exceeding 100k chars is truncated."""
        large_context = "x" * 150_000
        llm = _make_llm("ok")
        disc = Discussion("proposal", large_context, llm)

        await disc.respond("hello")
        # Verify system prompt was passed to LLM
        call_args = llm.call.call_args
        system_prompt = call_args[1]["system"]
        # Context is truncated to 100k chars
        assert len(system_prompt) < 110_000
        assert len(system_prompt) < 150_000
