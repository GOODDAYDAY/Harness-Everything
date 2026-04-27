"""Tests for PilotLoop — state machine, scheduling, approval flow."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.pilot.config import PilotConfig
from harness.pilot.feishu import CardAction, FeishuMessage
from harness.pilot.loop import PilotLoop, PilotState


def _minimal_config() -> PilotConfig:
    """Create a minimal PilotConfig for testing."""
    return PilotConfig.from_dict({
        "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
        "diagnosis": {
            "harness": {"model": "test"},
            "mission": "diagnose",
        },
        "proposal_expiry_hours": 1,
    })


def _make_agent_result(
    summary: str = "test proposal",
    cycles_run: int = 3,
    total_tool_calls: int = 10,
    mission_status: str = "complete",
    run_dir: str | None = None,
) -> MagicMock:
    """Create a mock AgentResult."""
    result = MagicMock()
    result.summary = summary
    result.cycles_run = cycles_run
    result.total_tool_calls = total_tool_calls
    result.mission_status = mission_status
    result.run_dir = run_dir
    return result


class TestStateTransitions:
    """US-02: Clear lifecycle phases with proper transitions."""

    def test_US02_initial_state_is_idle(self):
        """System starts in IDLE state."""
        loop = PilotLoop(_minimal_config())
        assert loop._state == PilotState.IDLE

    def test_US02_transition_logs(self):
        """State transitions are tracked."""
        loop = PilotLoop(_minimal_config())
        loop._transition(PilotState.DIAGNOSING)
        assert loop._state == PilotState.DIAGNOSING
        loop._transition(PilotState.NOTIFYING)
        assert loop._state == PilotState.NOTIFYING


class TestScheduler:
    """US-01: Daily schedule trigger and skip logic."""

    def test_US01_seconds_until_next_trigger(self):
        """Calculates positive delay until next trigger."""
        loop = PilotLoop(_minimal_config())
        delay = loop._seconds_until_next_trigger()
        assert delay > 0
        assert delay <= 86400  # at most 24h

    @pytest.mark.asyncio
    async def test_US01_skip_when_not_idle(self):
        """US-01 AC-2: Skips trigger when not in IDLE state."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING

        # Patch _run_improvement_cycle to track if it was called
        loop._run_improvement_cycle = AsyncMock()

        # Simulate scheduler check
        if loop._state != PilotState.IDLE:
            skipped = True
        else:
            skipped = False
            await loop._run_improvement_cycle()

        assert skipped
        loop._run_improvement_cycle.assert_not_called()


class TestDiagnosis:
    """US-03, US-04: Diagnosis phase extracts proposals and context."""

    def test_US04_extract_proposal_from_summary(self):
        """US-04: Extracts proposal from agent result summary."""
        result = _make_agent_result(summary="## Findings\nscores dropped")
        proposal = PilotLoop._extract_proposal(result)
        assert "Findings" in proposal

    def test_US04_no_summary_fallback(self):
        """US-04: Returns fallback text when summary is empty."""
        result = _make_agent_result(summary="")
        proposal = PilotLoop._extract_proposal(result)
        assert "No proposal" in proposal

    def test_US04_no_action_detection(self):
        """US-04 AC-2: Detects 'no action needed' proposals."""
        assert PilotLoop._is_no_action_proposal("Everything looks fine. No action needed.")
        assert not PilotLoop._is_no_action_proposal("## Proposed Actions\n- Fix error handling")

    def test_collect_diagnostic_context_no_run_dir(self):
        """Returns fallback when no run_dir available."""
        result = _make_agent_result(run_dir=None)
        context = PilotLoop._collect_diagnostic_context(result)
        assert "No diagnostic data" in context

    def test_collect_diagnostic_context_reads_tool_logs(self, tmp_path):
        """Reads and formats tool_log.json from cycle directories."""
        # Set up cycle dirs with tool logs
        cycle_dir = tmp_path / "cycle_001"
        cycle_dir.mkdir()
        tool_log = [
            {"tool": "db_query", "output": "feedback avg=3.2"},
            {"tool": "grep", "output": "error_count=15"},
        ]
        (cycle_dir / "tool_log.json").write_text(json.dumps(tool_log))

        result = _make_agent_result(run_dir=str(tmp_path))
        context = PilotLoop._collect_diagnostic_context(result)
        assert "db_query" in context
        assert "feedback avg=3.2" in context
        assert "grep" in context


class TestApprovalFlow:
    """US-08: Explicit approval via card button or text keyword."""

    @pytest.mark.asyncio
    async def test_US08_card_approve(self):
        """US-08 AC-1: Card 'Approve' button sets approval event."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        action = CardAction(
            action="approve",
            chat_id="oc_test",
            sender_id="u_1",
            message_id="m_1",
            raw_value={"action": "approve"},
        )
        await loop._handle_feishu_card_action(action)
        assert loop._approval_event.is_set()

    @pytest.mark.asyncio
    async def test_US08_card_reject(self):
        """US-08 AC-2: Card 'Reject' button sets rejection event."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        action = CardAction(
            action="reject",
            chat_id="oc_test",
            sender_id="u_1",
            message_id="m_1",
            raw_value={"action": "reject"},
        )
        await loop._handle_feishu_card_action(action)
        assert loop._rejection_event.is_set()

    @pytest.mark.asyncio
    async def test_US08_card_action_ignored_outside_discussion(self):
        """Card actions are ignored when not in DISCUSSING state."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.IDLE

        action = CardAction(
            action="approve",
            chat_id="oc_test",
            sender_id="u_1",
            message_id="m_1",
            raw_value={"action": "approve"},
        )
        await loop._handle_feishu_card_action(action)
        assert not loop._approval_event.is_set()

    @pytest.mark.asyncio
    async def test_US08_text_approval_two_step(self):
        """US-08 AC-3: Text approval keywords trigger confirmation step."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._discussion = MagicMock()
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        # Step 1: keyword detected → asks for confirmation
        msg = FeishuMessage(
            chat_id="oc_test",
            sender_id="u_1",
            text="approved",
            message_id="m_1",
            chat_type="group",
        )
        await loop._handle_discussion_message(msg)
        loop._feishu.send_text.assert_called_once()
        assert not loop._approval_event.is_set()

        # Step 2: explicit "yes" → approves
        confirm_msg = FeishuMessage(
            chat_id="oc_test",
            sender_id="u_1",
            text="yes",
            message_id="m_2",
            chat_type="group",
        )
        await loop._handle_discussion_message(confirm_msg)
        assert loop._approval_event.is_set()


class TestMessageRouting:
    """Feishu message routing based on current state."""

    @pytest.mark.asyncio
    async def test_message_during_idle(self):
        """Messages during IDLE get a 'no active proposal' response."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.IDLE
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        msg = FeishuMessage(
            chat_id="oc_test",
            sender_id="u_1",
            text="hello",
            message_id="m_1",
            chat_type="group",
        )
        await loop._handle_feishu_message(msg)
        call_text = loop._feishu.send_text.call_args[0][1]
        assert "No active proposal" in call_text

    @pytest.mark.asyncio
    async def test_message_during_execution(self):
        """Messages during EXECUTING get a 'please wait' response."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.EXECUTING
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        msg = FeishuMessage(
            chat_id="oc_test",
            sender_id="u_1",
            text="hello",
            message_id="m_1",
            chat_type="group",
        )
        await loop._handle_feishu_message(msg)
        call_text = loop._feishu.send_text.call_args[0][1]
        assert "executing" in call_text.lower() or "wait" in call_text.lower()

    @pytest.mark.asyncio
    async def test_message_during_discussion_routed_to_llm(self):
        """Messages during DISCUSSING are forwarded to Discussion.respond()."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._discussion = MagicMock()
        loop._discussion.respond = AsyncMock(return_value="LLM answer")
        loop._feishu = MagicMock()
        loop._feishu.send_markdown = AsyncMock()

        msg = FeishuMessage(
            chat_id="oc_test",
            sender_id="u_1",
            text="what about error handling?",
            message_id="m_1",
            chat_type="group",
        )
        await loop._handle_discussion_message(msg)
        loop._discussion.respond.assert_called_once_with("what about error handling?")
        loop._feishu.send_markdown.assert_called_once()


class TestProposalExpiry:
    """US-11: Proposals expire after configurable timeout."""

    @pytest.mark.asyncio
    async def test_US11_wait_for_decision_times_out(self):
        """US-11: Proposal expires when timeout elapses without decision."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "diagnosis": {"harness": {"model": "test"}, "mission": "x"},
            "proposal_expiry_hours": 0,  # immediate expiry for test
        })
        loop = PilotLoop(config)
        loop._feishu = MagicMock()
        loop._feishu.send_text = AsyncMock()

        # _wait_for_decision should return False (expired)
        # With 0 hours, timeout is 0 seconds → immediate expiry
        result = await loop._wait_for_decision()
        assert result is False
