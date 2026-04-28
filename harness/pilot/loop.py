"""PilotLoop — state machine orchestrating the daily improvement lifecycle.

Manages the full flow: schedule → diagnose → notify → discuss → execute → report.
Runs as a long-lived daemon process with Feishu WebSocket connectivity.

All project operations (branch, pull, squash, hash) are batch — every configured
project is treated equally.  State file lives in the process working directory,
not inside any project repo.
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import json
import logging
import signal
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from harness.pilot.config import PilotConfig
from harness.pilot.discussion import Discussion
from harness.pilot.feishu import (
    CardAction,
    FeishuClient,
    FeishuMessage,
    build_pilot_card,
)

log = logging.getLogger(__name__)

_NO_ACTION_MARKER = "no action needed"
_APPROVAL_KEYWORDS = frozenset({"approved", "approve", "go ahead", "lgtm", "通过", "批准"})
_STATE_FILENAME = ".pilot_state.json"
_MAX_PROPOSAL_HISTORY = 30


class PilotState(enum.Enum):
    """Lifecycle states for the pilot loop."""

    IDLE = "idle"
    DIAGNOSING = "diagnosing"
    NOTIFYING = "notifying"
    DISCUSSING = "discussing"
    EXECUTING = "executing"
    REPORTING = "reporting"


@dataclass
class PilotCycleRecord:
    """Complete record of one improvement cycle, persisted as JSON."""

    # Identity
    cycle_id: str = ""
    started_at: str = ""
    finished_at: str = ""

    # Phase timestamps (ISO 8601)
    diagnosed_at: str = ""
    notified_at: str = ""
    discussed_at: str = ""
    approved_at: str = ""
    executed_at: str = ""
    reported_at: str = ""

    # Outcome: no_action | rejected | expired | approved | error
    outcome: str = ""

    # Diagnosis
    proposal: str = ""
    diagnosis_run_dir: str = ""

    # Discussion
    discussion_messages: list[dict[str, str]] = field(default_factory=list)
    proposal_modified: bool = False
    final_proposal: str = ""

    # Approval
    approver_id: str = ""
    approval_method: str = ""  # card | text

    # Execution
    exec_branch: str = ""
    source_branches: dict[str, str] = field(default_factory=dict)
    execution_run_dir: str = ""
    execution_cycles: int = 0
    execution_tool_calls: int = 0
    execution_status: str = ""
    execution_summary: str = ""

    # Post-execution per-project checkpoints
    project_checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Feishu traceability
    card_message_id: str = ""

    # Error
    error: str = ""


class PilotLoop:
    """Orchestrates the daily improvement loop as a state machine.

    Collaborators: FeishuClient (notification), Discussion (LLM conversation),
    checkpoint meta-review (diagnosis), AgentLoop (execution).
    """

    def __init__(self, config: PilotConfig) -> None:
        self._config = config
        self._state = PilotState.IDLE
        self._feishu = FeishuClient(config.feishu.app_id, config.feishu.app_secret)
        self._discussion: Discussion | None = None
        self._proposal: str = ""
        self._proposal_timestamp: float = 0.0
        self._shutdown_event = asyncio.Event()
        self._approval_event = asyncio.Event()
        self._rejection_event = asyncio.Event()
        self._last_review_hashes: dict[str, str] = {}  # project_name → git hash
        self._card_message_id: str | None = None
        self._source_branches: dict[str, str] = {}  # project_name → original branch
        self._approver_id: str = ""  # Feishu user_id of who approved
        self._approval_via_card: bool = False
        self._exec_branch: str = ""

    async def run(self) -> None:
        """Start the pilot daemon: connect to Feishu, schedule daily runs.

        Blocks until shutdown signal (SIGTERM/SIGINT) is received.
        """
        # 1. Install signal handlers
        self._install_signal_handlers()

        # 2. Connect to Feishu
        await self._connect_feishu()

        # 3. Run scheduler loop
        await self._scheduler_loop()

        # 4. Clean up
        await self._shutdown()

    # ══════════════════════════════════════════════════════════════════════
    #  Lifecycle phases
    # ══════════════════════════════════════════════════════════════════════

    async def _run_improvement_cycle(self) -> None:
        """Execute one complete improvement cycle: diagnose → notify → discuss → execute → report."""
        now = datetime.now
        record = PilotCycleRecord(
            started_at=now().isoformat(),
            cycle_id=now().strftime("%Y%m%d-%H%M"),
        )

        try:
            # 1. Diagnose
            proposal, diagnostic_context, diag_run_dir = await self._run_diagnosis()
            record.diagnosed_at = now().isoformat()
            record.proposal = proposal
            record.diagnosis_run_dir = diag_run_dir

            # 2. No action needed?
            if self._is_no_action_proposal(proposal):
                await self._notify_no_action(proposal)
                self._save_proposal_record("no_action", proposal)
                record.outcome = "no_action"
                return

            # 3. Notify operator
            await self._notify_proposal(proposal)
            record.notified_at = now().isoformat()
            record.card_message_id = self._card_message_id or ""

            # 4. Discuss (blocks until approved/rejected/expired)
            approved, disc_msgs, current_prop, original_prop = await self._run_discussion(
                proposal, diagnostic_context,
            )
            record.discussed_at = now().isoformat()
            record.discussion_messages = disc_msgs
            if current_prop != original_prop:
                record.proposal_modified = True
                record.final_proposal = current_prop

            if not approved:
                self._save_proposal_record("rejected", proposal)
                record.outcome = "rejected"
                return

            # 5. Approved
            record.approved_at = now().isoformat()
            record.approver_id = self._approver_id
            record.approval_method = "card" if self._approval_via_card else "text"
            record.source_branches = dict(self._source_branches)

            # 6. Execute
            result = await self._run_execution()
            record.executed_at = now().isoformat()
            record.exec_branch = self._exec_branch
            record.execution_run_dir = result.run_dir or ""
            record.execution_cycles = result.cycles_run
            record.execution_tool_calls = result.total_tool_calls
            record.execution_status = result.mission_status
            record.execution_summary = result.summary or ""
            # Use exec branch as cycle_id (more precise than start time)
            if self._exec_branch.startswith("pilot/"):
                record.cycle_id = self._exec_branch.removeprefix("pilot/")

            # 7. Post-execution: squash + checkout back
            checkpoints = await self._post_execution_cleanup()
            record.project_checkpoints = checkpoints

            # 8. Report
            await self._report_results(result)
            record.reported_at = now().isoformat()

            self._save_proposal_record("approved", proposal)
            record.outcome = "approved"

        except Exception as exc:
            log.error("Improvement cycle failed: %s", exc, exc_info=True)
            await self._notify_error(str(exc))
            record.outcome = "error"
            record.error = str(exc)
        finally:
            if self._source_branches:
                try:
                    await self._checkout_branches(self._source_branches)
                except Exception:
                    log.warning("Failed to checkout back to source branches during cleanup")
            self._transition(PilotState.IDLE)
            self._save_cycle_record(record)
            self._run_post_cycle_hooks(record)

    # ── Phase: Diagnosis ──────────────────────────────────────────────────

    async def _run_diagnosis(self) -> tuple[str, str, str]:
        """Run a diagnosis agent to investigate production data and source code.

        Returns (proposal, diagnostic_context, run_dir).
        """
        self._transition(PilotState.DIAGNOSING)

        # 1. Capture source branch, pull latest code in all projects
        self._source_branches = await self._get_current_branches()
        await self._pull_latest()

        # 2. Build mission from template + proposal history + project list
        from harness.pilot.prompts import DIAGNOSIS_MISSION
        project_lines = []
        for p in self._config.projects:
            tools_note = f"（工具：{', '.join(p.tools.keys())}）" if p.tools else ""
            project_lines.append(f"- **{p.name}**: `{p.workspace}` {tools_note}")
        mission = DIAGNOSIS_MISSION.replace(
            "$project_list", "\n".join(project_lines),
        ).replace(
            "$proposal_history", self._format_proposal_history(),
        )

        # 3. Run diagnosis agent
        agent_config = self._config.build_diagnosis_agent_config(mission)
        result = await self._run_agent(agent_config)
        proposal = self._extract_structured_proposal((result.summary or "").strip())

        # 4. Update review hashes for all projects
        await self._save_all_review_hashes()

        diagnostic_context = (
            f"诊断 agent 运行了 {result.cycles_run} 轮，"
            f"使用了 {result.total_tool_calls} 次工具调用。"
        )
        log.info(
            "Diagnosis complete, branches=%s, proposal_len=%d, cycles=%d, tool_calls=%d",
            self._source_branch_label(), len(proposal), result.cycles_run, result.total_tool_calls,
        )
        return proposal, diagnostic_context, result.run_dir or ""

    # ── Phase: Notification ──────────────────────────────────────────────

    async def _notify_proposal(self, proposal: str) -> None:
        """Send the improvement proposal to Feishu as an interactive card."""
        self._transition(PilotState.NOTIFYING)
        self._proposal = proposal
        self._proposal_timestamp = time.time()
        card = build_pilot_card(proposal, self._source_branch_label(), status="discussing")
        self._card_message_id = await self._feishu.send_card(
            self._config.feishu.chat_id, card,
        )
        log.info("Proposal sent to Feishu, chat_id=%s", self._config.feishu.chat_id)

    async def _notify_no_action(self, summary: str) -> None:
        """Send a brief 'all clear' notification when no issues found."""
        card = build_pilot_card(summary, self._source_branch_label(), status="no_action")
        await self._feishu.send_card(self._config.feishu.chat_id, card)
        log.info("No-action notification sent")

    async def _notify_error(self, error_msg: str) -> None:
        """Notify the operator about a cycle failure.

        Truncates the error message to avoid leaking internal paths or
        stack traces into the Feishu chat.
        """
        sanitized = error_msg.split("\n")[0][:200]
        text = f"⚠️ 改进周期执行失败: {sanitized}"
        await self._feishu.send_text(self._config.feishu.chat_id, text)

    # ── Phase: Discussion ────────────────────────────────────────────────

    async def _run_discussion(
        self, proposal: str, diagnostic_context: str,
    ) -> tuple[bool, list[dict[str, Any]], str, str]:
        """Run the discussion phase: LLM-backed Q&A until approval or rejection.

        Returns (approved, messages, current_proposal, original_proposal).
        """
        self._transition(PilotState.DISCUSSING)

        # 1. Create discussion manager with LLM
        llm = self._create_discussion_llm()
        self._discussion = Discussion(proposal, diagnostic_context, llm)

        # 2. Reset approval/rejection events
        self._approval_event.clear()
        self._rejection_event.clear()
        self._approval_via_card = False

        # 3. Wait for approval, rejection, or expiry
        approved = await self._wait_for_decision()

        # 4. Capture discussion data before cleanup
        messages = list(self._discussion._messages) if self._discussion else []
        current = self._discussion.current_proposal if self._discussion else proposal
        original = self._discussion._original_proposal if self._discussion else proposal

        # 5. Clean up
        self._discussion = None

        return approved, messages, current, original

    async def _wait_for_decision(self) -> bool:
        """Block until the operator approves, rejects, or the proposal expires.

        Incoming Feishu messages are handled by callbacks registered in _connect_feishu().
        """
        expiry_seconds = self._config.proposal_expiry_hours * 3600
        try:
            # Wait for either approval or rejection, with timeout for expiry
            done, _ = await asyncio.wait(
                [
                    asyncio.create_task(self._approval_event.wait()),
                    asyncio.create_task(self._rejection_event.wait()),
                    asyncio.create_task(self._shutdown_event.wait()),
                ],
                timeout=expiry_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if self._shutdown_event.is_set():
                log.info("Discussion interrupted by shutdown")
                return False

            if self._approval_event.is_set():
                log.info("Proposal approved by operator")
                await self._update_card("approved")
                return True

            if self._rejection_event.is_set():
                log.info("Proposal rejected by operator")
                return False

            # Timeout — proposal expired
            log.info("Proposal expired after %dh", self._config.proposal_expiry_hours)
            await self._update_card("expired")
            return False

        except Exception as exc:
            log.error("Error waiting for decision: %s", exc)
            return False

    # ── Phase: Execution ─────────────────────────────────────────────────

    async def _run_execution(self) -> Any:
        """Run a harness agent with the approved proposal as mission.

        Creates a new branch in all projects for isolation before executing,
        so that changes can be reviewed and merged manually by the operator.
        """
        self._transition(PilotState.EXECUTING)

        # 1. Create isolated branch in all projects
        self._exec_branch = await self._create_exec_branch()
        await self._update_card("executing", {"exec_branch": self._exec_branch})

        # 2. Run agent on the new branch
        mission = self._discussion.current_proposal if self._discussion else self._proposal
        agent_config = self._config.build_execution_agent_config(mission)
        result = await self._run_agent(agent_config)

        log.info(
            "Execution complete, branch=%s, cycles=%d, tool_calls=%d, status=%s",
            self._exec_branch, result.cycles_run, result.total_tool_calls,
            result.mission_status,
        )
        return result

    async def _post_execution_cleanup(self) -> dict[str, dict[str, Any]]:
        """Squash commits in all projects.

        Checkout back to source branches is handled by the finally block in
        ``_run_improvement_cycle()`` as the single guaranteed cleanup point.

        Returns {project_name: {"squashed": bool, "head_hash": str}}.
        """
        from harness.agent import agent_eval

        llm = self._create_discussion_llm()
        checkpoints: dict[str, dict[str, Any]] = {}

        # 1. Squash each project independently
        for project in self._config.projects:
            repo_path = Path(project.workspace)
            since_hash = self._last_review_hashes.get(project.name, "")
            cp = await agent_eval.run_checkpoint(
                llm,
                cycle=0,
                score_history=[],
                since_hash=since_hash,
                current_notes="",
                repo_path=repo_path,
                write_fn=lambda *a, **kw: None,
                auto_squash=True,
                auto_tag=False,
            )
            if cp.squashed:
                log.info("Post-execution squash complete in %s on branch %s", project.name, self._exec_branch)
            self._last_review_hashes[project.name] = cp.head_hash
            checkpoints[project.name] = {
                "squashed": cp.squashed,
                "head_hash": cp.head_hash,
            }

        # 2. Persist updated hashes
        self._save_review_hashes(self._last_review_hashes)

        return checkpoints

    # ── Phase: Reporting ─────────────────────────────────────────────────

    async def _report_results(self, result: Any) -> None:
        """Update the pilot card and send a completion @mention to the approver."""
        self._transition(PilotState.REPORTING)
        await self._update_card("done", {
            "exec_branch": self._exec_branch,
            "cycles_run": result.cycles_run,
            "tool_calls": result.total_tool_calls,
            "mission_status": result.mission_status,
            "summary": result.summary or "",
        })

        # Send a separate @mention message to the approver
        mention = f"<at user_id=\"{self._approver_id}\">审批人</at> " if self._approver_id else ""
        status_label = "完成" if result.mission_status == "complete" else "结束"
        notify_text = (
            f"{mention}执行已{status_label}。\n"
            f"分支：{self._exec_branch}\n"
            f"执行轮次：{result.cycles_run}，工具调用：{result.total_tool_calls}\n"
            f"请检查变更并决定是否合并。"
        )
        await self._feishu.send_text(self._config.feishu.chat_id, notify_text)
        log.info("Execution report updated on card, approver notified")

    # ══════════════════════════════════════════════════════════════════════
    #  Feishu event handlers
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_feishu_message(self, msg: FeishuMessage) -> None:
        """Handle an incoming Feishu text message based on current state."""
        if self._state == PilotState.DISCUSSING and self._discussion:
            await self._handle_discussion_message(msg)
        elif self._state in (PilotState.DIAGNOSING, PilotState.EXECUTING):
            state_label = "正在诊断" if self._state == PilotState.DIAGNOSING else "正在执行"
            await self._feishu.send_text(
                msg.chat_id,
                f"⏳ {state_label}中，请稍候。",
            )
        else:
            await self._feishu.send_text(
                msg.chat_id,
                "💤 当前没有活跃提案。下次检查时间："
                f"{self._config.schedule.hour:02d}:{self._config.schedule.minute:02d}",
            )

    async def _handle_discussion_message(self, msg: FeishuMessage) -> None:
        """Process a message during the discussion phase."""
        # Acknowledge receipt with a reaction
        await self._feishu.add_reaction(msg.message_id)

        text_lower = msg.text.lower().strip()

        # Check for text-based approval
        if text_lower in _APPROVAL_KEYWORDS:
            await self._feishu.send_text(
                msg.chat_id,
                "检测到批准关键词，确认要执行吗？回复「确认」或点卡片上的「批准执行」按钮。",
            )
            return

        if text_lower in ("yes", "确认") and self._discussion:
            self._approver_id = msg.sender_id
            self._approval_event.set()
            return

        # Regular discussion message
        if self._discussion:
            response = await self._discussion.respond(msg.text)
            await self._feishu.send_markdown(msg.chat_id, response)

    async def _handle_feishu_card_action(self, action: CardAction) -> None:
        """Handle a card button click (approve/reject)."""
        if self._state != PilotState.DISCUSSING:
            log.warning("Card action received outside discussion state: %s", action.action)
            return

        if action.action == "approve":
            self._approver_id = action.sender_id
            self._approval_via_card = True
            self._approval_event.set()
        elif action.action == "reject":
            self._rejection_event.set()
            await self._update_card("rejected")
        else:
            log.warning("Unknown card action: %s", action.action)

    # ══════════════════════════════════════════════════════════════════════
    #  Infrastructure
    # ══════════════════════════════════════════════════════════════════════

    async def _connect_feishu(self) -> None:
        """Connect to Feishu and register event handlers."""
        self._feishu.on_message(self._handle_feishu_message)
        self._feishu.on_card_action(self._handle_feishu_card_action)
        await self._feishu.connect()
        log.info("Feishu connected, entering scheduler loop")

    async def _scheduler_loop(self) -> None:
        """Sleep until the next scheduled time, then run an improvement cycle.

        Respects shutdown signals and skips triggers if not in IDLE state.
        """
        while not self._shutdown_event.is_set():
            # 1. Calculate delay until next trigger
            delay = self._seconds_until_next_trigger()
            log.info(
                "Next trigger in %.0f seconds (%s)",
                delay,
                (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M"),
            )

            # 2. Wait (interruptible by shutdown)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=delay,
                )
                break  # shutdown triggered
            except asyncio.TimeoutError:
                pass  # timer fired

            # 3. Run improvement cycle if idle
            if self._state != PilotState.IDLE:
                log.warning("Skipping trigger: already in %s state", self._state.value)
                continue

            await self._run_improvement_cycle()

    async def _shutdown(self) -> None:
        """Gracefully shut down the pilot daemon."""
        log.info("Pilot shutting down")
        await self._feishu.close()
        log.info("Pilot shutdown complete")

    async def _run_agent(self, config_dict: dict[str, Any]) -> Any:
        """Create an AgentConfig and run AgentLoop programmatically."""
        from harness.agent.agent_loop import AgentConfig, AgentLoop

        agent_config = AgentConfig.from_dict(config_dict)
        loop = AgentLoop(agent_config)
        result = await loop.run()
        return result

    def _create_discussion_llm(self) -> Any:
        """Create a standalone LLM client for diagnosis and discussion."""
        from harness.core.config import HarnessConfig
        from harness.core.llm import LLM

        harness_cfg = HarnessConfig.from_dict(
            self._config.build_discussion_harness_config()
        )
        return LLM(harness_cfg)

    def _source_branch_label(self) -> str:
        """Human-readable label for the source branches (for Feishu cards)."""
        if not self._source_branches:
            return "unknown"
        unique = set(self._source_branches.values())
        if len(unique) == 1:
            return next(iter(unique))
        return ", ".join(f"{n}:{b}" for n, b in self._source_branches.items())

    def _resolve_workspaces(self) -> list[Path]:
        """Resolve all project workspace paths."""
        return [Path(p.workspace) for p in self._config.projects]

    async def _update_card(
        self, status: str, result: dict[str, Any] | None = None,
    ) -> None:
        """Update the pilot card to reflect a new lifecycle status."""
        if not self._card_message_id:
            log.warning("No card message_id to update")
            return
        card = build_pilot_card(
            self._proposal, self._source_branch_label(), status=status, result=result,
        )
        await self._feishu.update_card(self._card_message_id, card)

    async def _get_current_branches(self) -> dict[str, str]:
        """Get the current branch for every project.  Returns {project_name: branch}."""
        branches: dict[str, str] = {}
        for project in self._config.projects:
            repo = Path(project.workspace)
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            branches[project.name] = stdout.decode().strip() if stdout else "unknown"

        unique = set(branches.values())
        if len(unique) > 1:
            detail = ", ".join(f"{name}={b}" for name, b in branches.items())
            log.info("Projects on different branches: %s", detail)
        else:
            log.debug("All projects on branch: %s", next(iter(branches.values())))
        return branches

    async def _pull_latest(self) -> None:
        """Force pull the latest code from remote for all projects."""
        for project in self._config.projects:
            repo = Path(project.workspace)
            proc = await asyncio.create_subprocess_exec(
                "git", "pull", "--ff-only",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                branch = self._source_branches.get(project.name, "main")
                log.warning("git pull --ff-only failed in %s, trying reset to origin/%s", project.name, branch)
                proc2 = await asyncio.create_subprocess_exec(
                    "git", "reset", "--hard", f"origin/{branch}",
                    cwd=str(repo),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc2.communicate()
            log.info("Pulled latest in %s", project.name)

    async def _create_exec_branch(self) -> str:
        """Pull latest and create a new branch in all projects for execution isolation.

        Branch name format: pilot/YYYYMMDD-HHMM
        """
        await self._pull_latest()

        branch_name = f"pilot/{datetime.now().strftime('%Y%m%d-%H%M')}"
        for repo in self._resolve_workspaces():
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", "-b", branch_name,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode().strip() if stderr else "unknown error"
                log.error("Failed to create branch %s in %s: %s", branch_name, repo.name, err)
                raise RuntimeError(f"无法创建执行分支 {branch_name} ({repo.name}): {err}")
            log.info("Created execution branch %s in %s", branch_name, repo.name)
        return branch_name

    async def _checkout_branches(self, branches: dict[str, str]) -> None:
        """Checkout each project to its own branch.

        Args:
            branches: {project_name: branch_name} — each project checks out
                      its corresponding branch.
        """
        for project in self._config.projects:
            branch = branches.get(project.name, "")
            if not branch:
                log.warning("No branch recorded for %s, skipping checkout", project.name)
                continue
            repo = Path(project.workspace)
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", branch,
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode().strip() if stderr else "unknown error"
                log.warning("Failed to checkout %s in %s: %s", branch, project.name, err)
            else:
                log.info("Checked out %s in %s", branch, project.name)

    def _transition(self, new_state: PilotState) -> None:
        """Transition to a new state, logging the change."""
        old = self._state
        self._state = new_state
        log.info("State transition: %s → %s", old.value, new_state.value)

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._on_shutdown_signal, sig)
        log.debug("Signal handlers installed")

    def _on_shutdown_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal by setting the shutdown event."""
        log.info("Received signal %s, initiating shutdown", sig.name)
        self._shutdown_event.set()

    def _seconds_until_next_trigger(self) -> float:
        """Calculate seconds until the next scheduled trigger time."""
        now = datetime.now()
        target = now.replace(
            hour=self._config.schedule.hour,
            minute=self._config.schedule.minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    # ── Proposal state persistence ──────────────────────────────────────

    def _state_file_path(self) -> Path:
        """Path to the persistent pilot state file in the process working directory."""
        return Path.cwd() / _STATE_FILENAME

    def _load_state(self) -> dict[str, Any]:
        """Load pilot state from disk, returning empty dict if not found."""
        path = self._state_file_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load pilot state from %s: %s", path, exc)
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        """Write pilot state to disk."""
        path = self._state_file_path()
        try:
            path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            log.debug("Pilot state saved to %s", path)
        except Exception as exc:
            log.warning("Failed to save pilot state to %s: %s", path, exc)

    def _load_review_hashes(self) -> dict[str, str]:
        """Load per-project git hashes from the last diagnosis run."""
        state = self._load_state()
        # Migrate old single-hash format
        hashes = state.get("review_hashes", {})
        if not hashes and "last_review_hash" in state:
            old_hash = state["last_review_hash"]
            if old_hash and self._config.projects:
                hashes = {self._config.projects[0].name: old_hash}
        return hashes

    def _save_review_hashes(self, hashes: dict[str, str]) -> None:
        """Persist per-project git hashes after a diagnosis or execution run."""
        state = self._load_state()
        state["review_hashes"] = hashes
        state.pop("last_review_hash", None)  # clean up old format
        self._save_state(state)

    async def _save_all_review_hashes(self) -> None:
        """Get HEAD hash for every project and persist them."""
        from harness.agent import agent_git

        hashes: dict[str, str] = {}
        for project in self._config.projects:
            h = await agent_git.get_head_hash(Path(project.workspace))
            hashes[project.name] = h
        self._last_review_hashes = hashes
        self._save_review_hashes(hashes)

    def _save_proposal_record(self, status: str, proposal: str) -> None:
        """Append a proposal outcome to the history for future diagnosis context.

        Records date, status (approved/rejected/no_action/expired), and the
        first line of the proposal for quick identification.
        """
        state = self._load_state()
        history = state.get("proposal_history", [])
        history.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "status": status,
            "summary_first_line": proposal.split("\n")[0][:200],
        })
        # Keep bounded
        if len(history) > _MAX_PROPOSAL_HISTORY:
            history = history[-_MAX_PROPOSAL_HISTORY:]
        state["proposal_history"] = history
        self._save_state(state)
        log.info("Proposal record saved, status=%s, history_len=%d", status, len(history))

    def _save_cycle_record(self, record: PilotCycleRecord) -> None:
        """Save a complete cycle record as JSON to pilot_runs/."""
        record.finished_at = datetime.now().isoformat()
        runs_dir = Path.cwd() / "pilot_runs"
        runs_dir.mkdir(exist_ok=True)
        cycle_id = record.cycle_id or datetime.now().strftime("%Y%m%d-%H%M")
        path = runs_dir / f"{cycle_id}.json"
        try:
            path.write_text(
                json.dumps(asdict(record), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Cycle record saved: %s", path)
        except Exception as exc:
            log.warning("Failed to save cycle record to %s: %s", path, exc)

    def _run_post_cycle_hooks(self, record: PilotCycleRecord) -> None:
        """Scan pilot_hooks/ for Python files and invoke on_cycle_complete(record_dict).

        Non-invasive discovery pattern: drop a .py file into pilot_hooks/,
        it automatically takes effect.  No config changes needed.

        Each hook module must define ``on_cycle_complete(record: dict)``.
        Hooks run in sorted filename order.  Errors are logged but never
        propagate — a broken hook must not affect the main loop.
        """
        hooks_dir = Path.cwd() / "pilot_hooks"
        if not hooks_dir.is_dir():
            return

        hook_files = sorted(hooks_dir.glob("*.py"))
        if not hook_files:
            return

        record_dict = asdict(record)
        for hook_path in hook_files:
            module_name = f"pilot_hook_{hook_path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, hook_path)
                if spec is None or spec.loader is None:
                    log.warning("Cannot load hook %s: invalid module spec", hook_path.name)
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = getattr(mod, "on_cycle_complete", None)
                if fn is None:
                    log.warning("Hook %s has no on_cycle_complete() — skipped", hook_path.name)
                    continue
                fn(record_dict)
                log.info("Hook %s executed", hook_path.name)
            except Exception as exc:
                log.warning("Hook %s failed: %s", hook_path.name, exc)

    def _format_proposal_history(self) -> str:
        """Format proposal history as notes for the meta-review LLM.

        Gives the LLM context about what was previously proposed, approved,
        or rejected so it avoids re-proposing already-handled issues.
        """
        state = self._load_state()
        history = state.get("proposal_history", [])
        if not history:
            return "(No previous proposals)"

        lines = ["## Previous Proposals"]
        for entry in history[-10:]:
            lines.append(
                f"- [{entry['date']}] {entry['status']}: {entry['summary_first_line']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _extract_structured_proposal(text: str) -> str:
        """Extract only the structured proposal section from agent output.

        The diagnosis agent may include reasoning/thinking before the
        structured output.  We keep only from the first ``发现`` heading
        onwards (any markdown heading level: ##, ###, ####, etc.).
        Falls back to the full text if the marker is not found.
        """
        import re
        m = re.search(r"^#{2,4}\s+发现", text, re.MULTILINE)
        if m:
            return text[m.start():].strip()
        return text

    @staticmethod
    def _is_no_action_proposal(proposal: str) -> bool:
        """Check if the proposal indicates no action is needed."""
        return _NO_ACTION_MARKER in proposal.lower()
