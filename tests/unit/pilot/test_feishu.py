"""Tests for FeishuClient — message parsing, card builders, dedup."""

import json
from unittest.mock import MagicMock

import pytest

from harness.pilot.feishu import (
    CardAction,
    FeishuClient,
    FeishuMessage,
    build_pilot_card,
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
        for i in range(1100):
            client._is_duplicate(f"msg_{i}")
        assert client._is_duplicate("msg_0") is False
        assert client._is_duplicate("msg_1099") is True


class TestPilotCard:
    """build_pilot_card — single card for all lifecycle phases."""

    def test_discussing_card_has_buttons(self):
        """Discussing status includes approve/reject buttons and branch info."""
        card = build_pilot_card("## Findings\nscores dropped", "main", "discussing")
        assert card["header"]["title"]["content"] == "每日改进提案"
        elements = card["elements"]
        # Branch info
        assert any("`main`" in e.get("content", "") for e in elements)
        # Proposal content
        assert any("scores dropped" in e.get("content", "") for e in elements)
        # Action buttons
        action_el = [e for e in elements if e.get("tag") == "action"][0]
        actions = action_el["actions"]
        assert len(actions) == 2
        assert actions[0]["value"]["action"] == "approve"
        assert actions[1]["value"]["action"] == "reject"

    def test_approved_card_no_buttons(self):
        """Approved status removes buttons, shows approval message."""
        card = build_pilot_card("proposal text", "main", "approved")
        assert "已批准" in card["header"]["title"]["content"]
        assert not any(e.get("tag") == "action" for e in card["elements"])

    def test_executing_card_shows_branch(self):
        """Executing status shows the execution branch name."""
        card = build_pilot_card(
            "proposal text", "main", "executing",
            result={"exec_branch": "pilot/20260428-0918"},
        )
        assert "执行中" in card["header"]["title"]["content"]
        content = json.dumps(card, ensure_ascii=False)
        assert "pilot/20260428-0918" in content

    def test_done_card_shows_results(self):
        """Done status shows execution results and branch."""
        card = build_pilot_card(
            "proposal text", "main", "done",
            result={
                "exec_branch": "pilot/20260428-0918",
                "cycles_run": 30,
                "tool_calls": 150,
                "mission_status": "complete",
                "summary": "Fixed error handling",
            },
        )
        assert "已完成" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"
        content = json.dumps(card, ensure_ascii=False)
        assert "30" in content
        assert "150" in content
        assert "Fixed error handling" in content
        assert "pilot/20260428-0918" in content

    def test_rejected_card(self):
        """Rejected status shows rejection message."""
        card = build_pilot_card("proposal text", "main", "rejected")
        assert "已拒绝" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "red"
        assert not any(e.get("tag") == "action" for e in card["elements"])

    def test_expired_card(self):
        """Expired status shows expiry message."""
        card = build_pilot_card("proposal text", "main", "expired")
        assert "已过期" in card["header"]["title"]["content"]

    def test_no_action_card(self):
        """No-action card is brief and green."""
        card = build_pilot_card("All metrics normal", "main", "no_action")
        assert "一切正常" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"
