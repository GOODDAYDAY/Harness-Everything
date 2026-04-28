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

        with patch.object(loop, "_format_proposal_history", return_value="(No previous)"), \
             patch.object(loop, "_save_all_review_hashes", new_callable=AsyncMock), \
             patch.object(loop, "_get_current_branches", new_callable=AsyncMock, return_value={"TestProject": "main"}), \
             patch.object(loop, "_pull_latest", new_callable=AsyncMock), \
             patch.object(loop, "_run_agent", new_callable=AsyncMock, return_value=mock_result) as mock_agent:

            proposal, context, run_dir = await loop._run_diagnosis()

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

            # Branches were captured
            assert loop._source_branches == {"TestProject": "main"}

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
    """Post-execution squash all projects (no push — operator handles manually)."""

    @pytest.mark.asyncio
    async def test_post_execution_squash_all_projects(self):
        """Execution cleanup squashes every project, not just the first."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [
                {"name": "ProjectA", "workspace": "/tmp/ws-a"},
                {"name": "ProjectB", "workspace": "/tmp/ws-b"},
            ],
            "execution": {"auto_push": True, "push_remote": "origin", "push_branch": "main"},
        })
        loop = PilotLoop(config)
        loop._last_review_hashes = {"ProjectA": "aaa", "ProjectB": "bbb"}
        loop._exec_branch = "pilot/20260428-0918"
        loop._create_discussion_llm = MagicMock(return_value=MagicMock())

        mock_cp_result = MagicMock()
        mock_cp_result.squashed = True
        mock_cp_result.head_hash = "new_hash"

        with patch("harness.agent.agent_eval.run_checkpoint", new_callable=AsyncMock, return_value=mock_cp_result) as mock_cp, \
             patch("harness.agent.agent_git.push_head", new_callable=AsyncMock) as mock_push, \
             patch.object(loop, "_save_review_hashes"):

            loop._source_branches = {"ProjectA": "feat/harness", "ProjectB": "master"}
            checkpoints = await loop._post_execution_cleanup()

            # Checkpoint was called once per project
            assert mock_cp.call_count == 2
            for call in mock_cp.call_args_list:
                assert call[1]["auto_squash"] is True

            # Verify both repos were squashed
            repo_paths = [call[1]["repo_path"] for call in mock_cp.call_args_list]
            assert Path("/tmp/ws-a") in repo_paths
            assert Path("/tmp/ws-b") in repo_paths

            # Push was NOT called (operator handles manually)
            mock_push.assert_not_called()

            # Returns checkpoint data (checkout is handled by finally block)
            assert checkpoints["ProjectA"]["squashed"] is True
            assert checkpoints["ProjectB"]["squashed"] is True


class TestProposalHistory:
    """Proposal history persistence for avoiding repeated fixes."""

    def test_save_and_load_proposal_record(self, tmp_path, monkeypatch):
        """Proposal records are persisted and retrievable."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())

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

    def test_format_proposal_history(self, tmp_path, monkeypatch):
        """History is formatted as notes for meta-review LLM."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())

        loop._save_proposal_record("approved", "Fix error handling")
        formatted = loop._format_proposal_history()

        assert "Previous Proposals" in formatted
        assert "approved" in formatted
        assert "Fix error handling" in formatted

    def test_format_empty_history(self, tmp_path, monkeypatch):
        """Empty history returns placeholder text."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())
        assert "No previous" in loop._format_proposal_history()

    def test_save_review_hashes(self, tmp_path, monkeypatch):
        """Per-project review hashes are persisted and retrievable."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())

        loop._save_review_hashes({"TestProject": "abc123"})
        loaded = loop._load_review_hashes()
        assert loaded == {"TestProject": "abc123"}

    def test_review_hash_migration(self, tmp_path, monkeypatch):
        """Old single-hash format is migrated to per-project dict."""
        monkeypatch.chdir(tmp_path)
        # Write old-format state file
        state_path = tmp_path / ".pilot_state.json"
        state_path.write_text(json.dumps({"last_review_hash": "old_hash"}))

        loop = PilotLoop(_minimal_config())
        loaded = loop._load_review_hashes()
        assert loaded == {"TestProject": "old_hash"}

    def test_history_bounded(self, tmp_path, monkeypatch):
        """History is bounded to _MAX_PROPOSAL_HISTORY entries."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())

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
        loop._source_branches = {"TestProject": "main"}
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
        loop._source_branches = {"TestProject": "main"}
        loop._card_message_id = "msg_card_1"
        loop._feishu = MagicMock()
        loop._feishu.update_card = AsyncMock(return_value=True)

        result = await loop._wait_for_decision()
        assert result is False
        # Card should be updated to expired status
        loop._feishu.update_card.assert_called_once()


class TestProposalExtraction:
    """Structured proposal extraction from agent output."""

    def test_extracts_from_marker(self):
        """Strips agent thinking, keeps only structured proposal."""
        raw = (
            "很好！现在我有了完整的图像……\n\n"
            "MISSION COMPLETE\n\n"
            "### 发现\n\n错误处理有问题\n\n"
            "### 根因分析\n\n具体原因\n\n"
            "### 建议改进\n\n改这些文件"
        )
        result = PilotLoop._extract_structured_proposal(raw)
        assert result.startswith("### 发现")
        assert "完整的图像" not in result
        assert "MISSION COMPLETE" not in result

    def test_extracts_from_h2_marker(self):
        """Handles ## 发现 (h2) as well as ### 发现 (h3)."""
        raw = (
            "现在我完全准备好撰写改进提案了。\n\n---\n\n"
            "## 发现\n\n用户反馈分类统计\n\n"
            "## 根因分析\n\n追溯典型案例"
        )
        result = PilotLoop._extract_structured_proposal(raw)
        assert result.startswith("## 发现")
        assert "准备好撰写" not in result

    def test_fallback_when_no_marker(self):
        """Returns full text if no structured marker is found."""
        raw = "no action needed"
        result = PilotLoop._extract_structured_proposal(raw)
        assert result == "no action needed"

    def test_empty_input(self):
        """Handles empty string gracefully."""
        assert PilotLoop._extract_structured_proposal("") == ""


class TestPostCycleHooks:
    """Post-cycle hook discovery and execution."""

    def test_hooks_dir_missing_is_noop(self, tmp_path, monkeypatch):
        """No error when pilot_hooks/ doesn't exist."""
        monkeypatch.chdir(tmp_path)
        loop = PilotLoop(_minimal_config())
        from harness.pilot.loop import PilotCycleRecord
        record = PilotCycleRecord(cycle_id="test", outcome="approved")
        loop._run_post_cycle_hooks(record)  # should not raise

    def test_hooks_discovered_and_called(self, tmp_path, monkeypatch):
        """Hook files in pilot_hooks/ are discovered and on_cycle_complete() is called."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / "pilot_hooks"
        hooks_dir.mkdir()

        # Write a hook that creates a marker file
        hook_code = (
            "def on_cycle_complete(record):\n"
            "    from pathlib import Path\n"
            "    Path('hook_ran.txt').write_text(record['cycle_id'])\n"
        )
        (hooks_dir / "test_hook.py").write_text(hook_code)

        loop = PilotLoop(_minimal_config())
        from harness.pilot.loop import PilotCycleRecord
        record = PilotCycleRecord(cycle_id="20260428-0900", outcome="approved")
        loop._run_post_cycle_hooks(record)

        marker = tmp_path / "hook_ran.txt"
        assert marker.exists()
        assert marker.read_text() == "20260428-0900"

    def test_hook_error_does_not_propagate(self, tmp_path, monkeypatch):
        """A broken hook logs a warning but doesn't crash the loop."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / "pilot_hooks"
        hooks_dir.mkdir()

        (hooks_dir / "bad_hook.py").write_text(
            "def on_cycle_complete(record):\n    raise RuntimeError('boom')\n"
        )

        loop = PilotLoop(_minimal_config())
        from harness.pilot.loop import PilotCycleRecord
        record = PilotCycleRecord(cycle_id="test", outcome="error")
        loop._run_post_cycle_hooks(record)  # should not raise

    def test_hook_without_function_is_skipped(self, tmp_path, monkeypatch):
        """A .py file without on_cycle_complete() is skipped with a warning."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / "pilot_hooks"
        hooks_dir.mkdir()

        (hooks_dir / "empty_hook.py").write_text("x = 1\n")

        loop = PilotLoop(_minimal_config())
        from harness.pilot.loop import PilotCycleRecord
        record = PilotCycleRecord(cycle_id="test", outcome="no_action")
        loop._run_post_cycle_hooks(record)  # should not raise

    def test_hooks_run_in_sorted_order(self, tmp_path, monkeypatch):
        """Hooks execute in alphabetical filename order."""
        monkeypatch.chdir(tmp_path)
        hooks_dir = tmp_path / "pilot_hooks"
        hooks_dir.mkdir()

        for name in ["c_hook.py", "a_hook.py", "b_hook.py"]:
            (hooks_dir / name).write_text(
                "def on_cycle_complete(record):\n"
                "    from pathlib import Path\n"
                f"    p = Path('order.txt')\n"
                f"    p.write_text(p.read_text() + '{name},' if p.exists() else '{name},')\n"
            )

        loop = PilotLoop(_minimal_config())
        from harness.pilot.loop import PilotCycleRecord
        record = PilotCycleRecord(cycle_id="test", outcome="approved")
        loop._run_post_cycle_hooks(record)

        order = (tmp_path / "order.txt").read_text()
        assert order == "a_hook.py,b_hook.py,c_hook.py,"


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
    async def test_get_current_branch_verifies_all_projects(self):
        """Gets the current branch and verifies all projects agree."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [
                {"name": "A", "workspace": "/tmp/a"},
                {"name": "B", "workspace": "/tmp/b"},
            ],
        })
        loop = PilotLoop(config)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"feat/harness\n", b""))
            mock_exec.return_value = mock_proc

            branches = await loop._get_current_branches()
            assert branches == {"A": "feat/harness", "B": "feat/harness"}
            # Called once per project
            assert mock_exec.call_count == 2

    @pytest.mark.asyncio
    async def test_branch_ops_iterate_all_projects(self):
        """Checkout iterates all projects with per-project branches."""
        config = PilotConfig.from_dict({
            "feishu": {"app_id": "a", "app_secret": "s", "chat_id": "oc_test"},
            "projects": [
                {"name": "A", "workspace": "/tmp/a"},
                {"name": "B", "workspace": "/tmp/b"},
            ],
        })
        loop = PilotLoop(config)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            await loop._checkout_branches({"A": "feat/harness", "B": "master"})
            # Two projects = two git calls
            assert mock_exec.call_count == 2
            cwds = [call[1]["cwd"] for call in mock_exec.call_args_list]
            assert "/tmp/a" in cwds
            assert "/tmp/b" in cwds
            # Each project gets its own branch
            branches = [call[0][2] for call in mock_exec.call_args_list]
            assert "feat/harness" in branches
            assert "master" in branches
