"""Tests for FeishuClient — message parsing, card builders, dedup."""

import json
from unittest.mock import MagicMock

import pytest

from harness.pilot.feishu import (
    CardAction,
    FeishuClient,
    FeishuMessage,
    build_no_action_card,
    build_proposal_card,
    build_report_card,
)


class TestMessageParsing:
    """FeishuMessage extraction from raw WebSocket events."""

    def _make_event(
        self,
        text: str = "hello",
        sender_type: str = "user",
        msg_type: str = "text",
        chat_id: str = "oc_test",
    ) -> MagicMock:
        """Build a mock lark-oapi message event."""
        data = MagicMock()
        event = MagicMock()
        message = MagicMock()
        sender = MagicMock()

        sender.sender_type = sender_type
        sender.sender_id = MagicMock()
        sender.sender_id.open_id = "ou_123"

        message.message_type = msg_type
        message.content = json.dumps({"text": text})
        message.chat_id = chat_id
        message.message_id = "msg_001"
        message.chat_type = "group"

        event.message = message
        event.sender = sender
        data.event = event
        return data

    def test_parses_text_message(self):
        """Extracts text, chat_id, sender_id from valid event."""
        data = self._make_event(text="test message")
        msg = FeishuClient._parse_message_event(data)
        assert msg is not None
        assert msg.text == "test message"
        assert msg.chat_id == "oc_test"
        assert msg.sender_id == "ou_123"

    def test_skips_bot_messages(self):
        """Bot messages are filtered out."""
        data = self._make_event(sender_type="bot")
        msg = FeishuClient._parse_message_event(data)
        assert msg is None

    def test_skips_non_text_messages(self):
        """Non-text message types are filtered out."""
        data = self._make_event(msg_type="image")
        msg = FeishuClient._parse_message_event(data)
        assert msg is None

    def test_skips_empty_text(self):
        """Empty text content is filtered out."""
        data = self._make_event(text="   ")
        msg = FeishuClient._parse_message_event(data)
        assert msg is None


class TestCardActionParsing:
    """CardAction extraction from raw card events."""

    def _make_card_event(
        self, action_name: str = "approve", chat_id: str = "oc_test"
    ) -> MagicMock:
        """Build a mock card action event."""
        data = MagicMock()
        event = MagicMock()
        action = MagicMock()
        action.value = {"action": action_name}

        ctx = MagicMock()
        ctx.open_chat_id = chat_id
        ctx.open_message_id = "msg_card"

        operator = MagicMock()
        operator.open_id = "ou_456"

        event.action = action
        event.context = ctx
        event.operator = operator
        data.event = event
        return data

    def test_parses_approve_action(self):
        """Extracts approve action from card event."""
        data = self._make_card_event("approve")
        result = FeishuClient._parse_card_action_event(data)
        assert result is not None
        assert result.action == "approve"
        assert result.chat_id == "oc_test"

    def test_parses_reject_action(self):
        """Extracts reject action from card event."""
        data = self._make_card_event("reject")
        result = FeishuClient._parse_card_action_event(data)
        assert result is not None
        assert result.action == "reject"

    def test_missing_action_returns_none(self):
        """Returns None when event has no action."""
        data = MagicMock()
        data.event = MagicMock()
        data.event.action = None
        result = FeishuClient._parse_card_action_event(data)
        assert result is None


class TestDeduplication:
    """Message deduplication cache."""

    def test_first_message_not_duplicate(self):
        """First occurrence of a message_id is not duplicate."""
        client = FeishuClient("a", "s")
        assert client._is_duplicate("msg_001") is False

    def test_second_message_is_duplicate(self):
        """Second occurrence of same message_id is duplicate."""
        client = FeishuClient("a", "s")
        client._is_duplicate("msg_001")
        assert client._is_duplicate("msg_001") is True

    def test_cache_eviction(self):
        """Oldest entries evicted when cache exceeds limit."""
        client = FeishuClient("a", "s")
        # Fill cache beyond limit
        for i in range(1100):
            client._is_duplicate(f"msg_{i}")
        # First entries should have been evicted
        assert client._is_duplicate("msg_0") is False
        # Recent entries should still be cached
        assert client._is_duplicate("msg_1099") is True


class TestCardBuilders:
    """US-05: Feishu card construction."""

    def test_US05_proposal_card_structure(self):
        """US-05 AC-1: Proposal card has header, content, approve/reject buttons."""
        card = build_proposal_card("## Findings\nscores dropped")
        assert card["header"]["title"]["content"] == "Daily Improvement Proposal"
        elements = card["elements"]
        # First element is markdown content
        assert elements[0]["tag"] == "markdown"
        assert "scores dropped" in elements[0]["content"]
        # Second element has action buttons
        actions = elements[1]["actions"]
        assert len(actions) == 2
        assert actions[0]["value"]["action"] == "approve"
        assert actions[1]["value"]["action"] == "reject"

    def test_US05_report_card_complete_status(self):
        """US-10: Report card shows execution summary."""
        card = build_report_card(
            cycles_run=30,
            total_tool_calls=150,
            mission_status="complete",
            summary="Fixed error handling in dispatcher",
        )
        body = card["elements"][0]["content"]
        assert "30" in body
        assert "150" in body
        assert "complete" in body
        assert "Fixed error handling" in body
        assert card["header"]["template"] == "green"

    def test_report_card_incomplete_status(self):
        """Report card uses orange template for incomplete execution."""
        card = build_report_card(
            cycles_run=50,
            total_tool_calls=200,
            mission_status="cycle_limit",
            summary="Partial progress",
        )
        assert card["header"]["template"] == "orange"

    def test_US05_no_action_card(self):
        """US-05 AC-3: No-action card is brief and green."""
        card = build_no_action_card("All metrics within normal range.")
        assert "All Clear" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"
        assert "normal range" in card["elements"][0]["content"]
