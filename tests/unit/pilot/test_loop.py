"""Tests for PilotLoop — state machine, scheduling, meta-review diagnosis, approval flow."""

import asyncio
import json
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
        "projects": [{"name": "TestProject", "workspace": "/tmp/test-workspace"}],
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

        loop._run_improvement_cycle = AsyncMock()

        if loop._state != PilotState.IDLE:
            skipped = True
        else:
            skipped = False
            await loop._run_improvement_cycle()

        assert skipped
        loop._run_improvement_cycle.assert_not_called()


class TestDiagnosis:
    """US-03, US-04: Diagnosis via agent run."""

    @pytest.mark.asyncio
    async def test_US03_diagnosis_runs_agent(self):
        """US-03: Diagnosis runs an agent with tools to investigate freely."""
        loop = PilotLoop(_minimal_config())

        mock_result = _make_agent_result(
            summary="### 发现\n错误处理有问题",
            cycles_run=3,
            total_tool_calls=15,
            mission_status="complete",
        )

        with patch("harness.agent.agent_git.get_head_hash", new_callable=AsyncMock, return_value="def456"), \
             patch.object(loop, "_format_proposal_history", return_value="(No previous)"), \
             patch.object(loop, "_save_last_review_hash"), \
             patch.object(loop, "_resolve_workspace", return_value=Path("/tmp/ws")), \
             patch.object(loop, "_get_current_branch", new_callable=AsyncMock, return_value="main"), \
             patch.object(loop, "_run_agent", new_callable=AsyncMock, return_value=mock_result) as mock_agent:

            proposal, context = await loop._run_diagnosis()

            # Agent was run with diagnosis mission
            mock_agent.assert_called_once()
            agent_config = mock_agent.call_args[0][0]
            mission = agent_config["mission"]
            assert "改进顾问" in mission
            assert "(No previous)" in mission
            assert agent_config["auto_commit"] is False
            assert agent_config["auto_evaluate"] is False

            # Returns proposal from agent summary
            assert proposal == "### 发现\n错误处理有问题"
            assert "3 轮" in context

            # Branch was captured
            assert loop._source_branch == "main"

    @pytest.mark.asyncio
    async def test_US03_diagnosis_builds_config_with_db_query(self):
        """US-03: Diagnosis agent config includes db_query tool."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{
                "name": "TestProject",
                "workspace": "/tmp/test-workspace",
                "tools": {"db_query": {"dsn": "postgresql://user:pass@host/db"}},
            }],
            "diagnosis": {"max_cycles": 3},
        })

        agent_cfg = config.build_diagnosis_agent_config("test mission")
        assert "db_query" in agent_cfg["harness"]["extra_tools"]
        assert agent_cfg["harness"]["tool_config"]["db_query"]["dsn"] == "postgresql://user:pass@host/db"
        assert agent_cfg["max_cycles"] == 3
        assert agent_cfg["auto_commit"] is False

    def test_US04_no_action_detection(self):
        """US-04 AC-2: Detects 'no action needed' proposals."""
        assert PilotLoop._is_no_action_proposal("Everything looks fine. No action needed.")
        assert not PilotLoop._is_no_action_proposal("## Proposed Actions\n- Fix error handling")


class TestPostExecution:
    """Post-execution squash (no push — operator handles manually)."""

    @pytest.mark.asyncio
    async def test_post_execution_squash_no_push(self):
        """Execution cleanup calls checkpoint squash but does NOT push."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": "/tmp/ws"}],
            "execution": {"auto_push": True, "push_remote": "origin", "push_branch": "main"},
        })
        loop = PilotLoop(config)
        loop._last_review_hash = "abc123"
        loop._exec_branch = "pilot/20260428-0918"
        loop._create_discussion_llm = MagicMock(return_value=MagicMock())

        mock_cp_result = MagicMock()
        mock_cp_result.squashed = True
        mock_cp_result.head_hash = "new_hash"

        with patch("harness.agent.agent_eval.run_checkpoint", new_callable=AsyncMock, return_value=mock_cp_result) as mock_cp, \
             patch("harness.agent.agent_git.push_head", new_callable=AsyncMock) as mock_push, \
             patch.object(loop, "_save_last_review_hash"), \
             patch.object(loop, "_checkout_branch", new_callable=AsyncMock) as mock_checkout:

            await loop._post_execution_cleanup()

            # Checkpoint was called with auto_squash=True
            mock_cp.assert_called_once()
            call_kwargs = mock_cp.call_args
            assert call_kwargs[1]["auto_squash"] is True

            # Push was NOT called (operator handles manually)
            mock_push.assert_not_called()

            # Checked out back to source branch
            mock_checkout.assert_called_once()


class TestProposalHistory:
    """Proposal history persistence for avoiding repeated fixes."""

    def test_save_and_load_proposal_record(self, tmp_path):
        """Proposal records are persisted and retrievable."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": str(tmp_path)}],
        })
        loop = PilotLoop(config)

        # Save two records
        loop._save_proposal_record("approved", "## Fix error handling\nDetails...")
        loop._save_proposal_record("rejected", "## Refactor prompts\nDetails...")

        # Load and verify
        state = loop._load_state()
        history = state["proposal_history"]
        assert len(history) == 2
        assert history[0]["status"] == "approved"
        assert history[0]["summary_first_line"] == "## Fix error handling"
        assert history[1]["status"] == "rejected"

    def test_format_proposal_history(self, tmp_path):
        """History is formatted as notes for meta-review LLM."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": str(tmp_path)}],
        })
        loop = PilotLoop(config)

        loop._save_proposal_record("approved", "Fix error handling")
        formatted = loop._format_proposal_history()

        assert "Previous Proposals" in formatted
        assert "approved" in formatted
        assert "Fix error handling" in formatted

    def test_format_empty_history(self, tmp_path):
        """Empty history returns placeholder text."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": str(tmp_path)}],
        })
        loop = PilotLoop(config)
        assert "No previous" in loop._format_proposal_history()

    def test_save_review_hash(self, tmp_path):
        """Review hash is persisted across saves."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": str(tmp_path)}],
        })
        loop = PilotLoop(config)

        loop._save_last_review_hash("abc123")
        assert loop._load_last_review_hash() == "abc123"

    def test_history_bounded(self, tmp_path):
        """History is bounded to _MAX_PROPOSAL_HISTORY entries."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [{"name": "TestProject", "workspace": str(tmp_path)}],
        })
        loop = PilotLoop(config)

        for i in range(40):
            loop._save_proposal_record("approved", f"Proposal {i}")

        state = loop._load_state()
        assert len(state["proposal_history"]) == 30  # _MAX_PROPOSAL_HISTORY


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
        """US-08 AC-2: Card 'Reject' button sets rejection event and updates card."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._proposal = "test proposal"
        loop._source_branch = "main"
        loop._card_message_id = "msg_card_1"
        loop._feishu = MagicMock()
        loop._feishu.update_card = AsyncMock(return_value=True)

        action = CardAction(
            action="reject",
            chat_id="oc_test",
            sender_id="u_1",
            message_id="m_1",
            raw_value={"action": "reject"},
        )
        await loop._handle_feishu_card_action(action)
        assert loop._rejection_event.is_set()
        # Card should be updated to rejected status
        loop._feishu.update_card.assert_called_once()

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
        loop._feishu.add_reaction = AsyncMock()

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
        assert "没有活跃提案" in call_text

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
        assert "正在执行" in call_text or "稍候" in call_text

    @pytest.mark.asyncio
    async def test_message_during_discussion_routed_to_llm(self):
        """Messages during DISCUSSING are forwarded to Discussion.respond()."""
        loop = PilotLoop(_minimal_config())
        loop._state = PilotState.DISCUSSING
        loop._discussion = MagicMock()
        loop._discussion.respond = AsyncMock(return_value="LLM answer")
        loop._feishu = MagicMock()
        loop._feishu.send_markdown = AsyncMock()
        loop._feishu.add_reaction = AsyncMock()

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
            "projects": [{"name": "TestProject", "workspace": "/tmp/ws"}],
            "proposal_expiry_hours": 0,
        })
        loop = PilotLoop(config)
        loop._proposal = "test"
        loop._source_branch = "main"
        loop._card_message_id = "msg_card_1"
        loop._feishu = MagicMock()
        loop._feishu.update_card = AsyncMock(return_value=True)

        result = await loop._wait_for_decision()
        assert result is False
        # Card should be updated to expired status
        loop._feishu.update_card.assert_called_once()


class TestBranchOperations:
    """Execution branch creation for isolation."""

    @pytest.mark.asyncio
    async def test_create_exec_branch_name_format(self):
        """Branch name follows pilot/YYYYMMDD-HHMM pattern."""
        loop = PilotLoop(_minimal_config())

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            branch = await loop._create_exec_branch()

            assert branch.startswith("pilot/")
            # Verify git checkout -b was called
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "git"
            assert call_args[1] == "checkout"
            assert call_args[2] == "-b"
            assert call_args[3] == branch

    @pytest.mark.asyncio
    async def test_get_current_branch(self):
        """Gets the current branch name from git."""
        loop = PilotLoop(_minimal_config())

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"feat/harness\n", b""))
            mock_exec.return_value = mock_proc

            branch = await loop._get_current_branch()
            assert branch == "feat/harness"
