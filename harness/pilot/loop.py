"""PilotLoop — state machine orchestrating the daily improvement lifecycle.

Manages the full flow: schedule → diagnose → notify → discuss → execute → report.
Runs as a long-lived daemon process with Feishu WebSocket connectivity.

Diagnosis reuses the harness checkpoint meta-review mechanism (read-only analysis
of score trends + git delta).  Post-execution reuses checkpoint squash + push.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from harness.pilot.config import PilotConfig
from harness.pilot.discussion import Discussion
from harness.pilot.feishu import (
    CardAction,
    FeishuClient,
    FeishuMessage,
    build_no_action_card,
    build_proposal_card,
    build_report_card,
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
        self._last_review_hash: str = ""

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
        try:
            # 1. Diagnose via meta-review
            proposal, diagnostic_context = await self._run_diagnosis()

            # 2. Check if action is needed
            if self._is_no_action_proposal(proposal):
                await self._notify_no_action(proposal)
                self._save_proposal_record("no_action", proposal)
                return

            # 3. Notify operator via Feishu
            await self._notify_proposal(proposal)

            # 4. Discuss with operator (blocks until approved/rejected/expired)
            approved = await self._run_discussion(proposal, diagnostic_context)
            if not approved:
                self._save_proposal_record("rejected", proposal)
                return

            # 5. Execute the approved plan
            result = await self._run_execution()

            # 6. Post-execution: squash + push
            await self._post_execution_cleanup()

            # 7. Report results
            await self._report_results(result)

            # 8. Record approval
            self._save_proposal_record("approved", proposal)

        except Exception as exc:
            log.error("Improvement cycle failed: %s", exc, exc_info=True)
            await self._notify_error(str(exc))
        finally:
            self._transition(PilotState.IDLE)

    # ── Phase: Diagnosis (meta-review) ──────────────────────────────────

    async def _run_diagnosis(self) -> tuple[str, str]:
        """Run a checkpoint-style meta-review to produce an improvement proposal.

        Reuses the harness meta-review mechanism: analyzes git delta and
        proposal history to identify improvement opportunities.  No code
        changes are made — this is a read-only analysis.
        """
        self._transition(PilotState.DIAGNOSING)

        # 1. Resolve workspace and load persistent state
        repo_path = self._resolve_workspace()
        since_hash = self._load_last_review_hash()
        proposal_notes = self._format_proposal_history()

        # 2. Gather git delta as diagnostic context
        from harness.agent import agent_git
        git_delta = await agent_git.get_review_git_delta(
            repo_path, since_hash or "HEAD~20",
        )

        # 3. Format score history (empty — pilot doesn't track eval scores)
        from harness.agent.agent_eval import format_score_history
        score_table = format_score_history([])

        # 4. Run meta-review LLM
        from harness.agent.agent_eval import _meta_review_llm
        llm = self._create_discussion_llm()
        proposal = await _meta_review_llm(llm, score_table, git_delta, proposal_notes)

        # 5. Update review hash for next run
        head_hash = await agent_git.get_head_hash(repo_path)
        self._save_last_review_hash(head_hash)
        self._last_review_hash = head_hash

        log.info(
            "Diagnosis complete, proposal_len=%d, context_len=%d",
            len(proposal), len(git_delta),
        )
        return proposal, git_delta

    # ── Phase: Notification ──────────────────────────────────────────────

    async def _notify_proposal(self, proposal: str) -> None:
        """Send the improvement proposal to Feishu as an interactive card."""
        self._transition(PilotState.NOTIFYING)
        self._proposal = proposal
        self._proposal_timestamp = time.time()
        card = build_proposal_card(proposal)
        await self._feishu.send_card(self._config.feishu.chat_id, card)
        log.info("Proposal sent to Feishu, chat_id=%s", self._config.feishu.chat_id)

    async def _notify_no_action(self, summary: str) -> None:
        """Send a brief 'all clear' notification when no issues found."""
        card = build_no_action_card(summary)
        await self._feishu.send_card(self._config.feishu.chat_id, card)
        log.info("No-action notification sent")

    async def _notify_error(self, error_msg: str) -> None:
        """Notify the operator about a cycle failure.

        Truncates the error message to avoid leaking internal paths or
        stack traces into the Feishu chat.
        """
        sanitized = error_msg.split("\n")[0][:200]
        text = f"⚠️ Improvement cycle failed: {sanitized}"
        await self._feishu.send_text(self._config.feishu.chat_id, text)

    # ── Phase: Discussion ────────────────────────────────────────────────

    async def _run_discussion(self, proposal: str, diagnostic_context: str) -> bool:
        """Run the discussion phase: LLM-backed Q&A until approval or rejection.

        Returns True if approved, False if rejected or expired.
        """
        self._transition(PilotState.DISCUSSING)

        # 1. Create discussion manager with LLM
        llm = self._create_discussion_llm()
        self._discussion = Discussion(proposal, diagnostic_context, llm)

        # 2. Reset approval/rejection events
        self._approval_event.clear()
        self._rejection_event.clear()

        # 3. Wait for approval, rejection, or expiry
        approved = await self._wait_for_decision()

        # 4. Clean up
        self._discussion = None

        return approved

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
                await self._feishu.send_text(
                    self._config.feishu.chat_id,
                    "✅ Proposal approved. Starting execution...",
                )
                return True

            if self._rejection_event.is_set():
                log.info("Proposal rejected by operator")
                return False

            # Timeout — proposal expired
            log.info("Proposal expired after %dh", self._config.proposal_expiry_hours)
            await self._feishu.send_text(
                self._config.feishu.chat_id,
                f"⏰ Proposal expired (no response within {self._config.proposal_expiry_hours}h).",
            )
            return False

        except Exception as exc:
            log.error("Error waiting for decision: %s", exc)
            return False

    # ── Phase: Execution ─────────────────────────────────────────────────

    async def _run_execution(self) -> Any:
        """Run a harness agent with the approved proposal as mission."""
        self._transition(PilotState.EXECUTING)

        mission = self._discussion.current_proposal if self._discussion else self._proposal
        agent_config = self._config.build_execution_agent_config(mission)
        result = await self._run_agent(agent_config)

        log.info(
            "Execution complete, cycles=%d, tool_calls=%d, status=%s",
            result.cycles_run, result.total_tool_calls, result.mission_status,
        )
        return result

    async def _post_execution_cleanup(self) -> None:
        """Squash commits and push to remote after execution completes.

        Reuses the harness checkpoint mechanism for squash grouping.
        """
        from harness.agent import agent_eval, agent_git

        repo_path = self._resolve_workspace()
        since_hash = self._last_review_hash

        # 1. Squash via checkpoint (meta-review output is discarded)
        llm = self._create_discussion_llm()
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
            log.info("Post-execution squash complete")

        # 2. Push to remote
        execution = self._config.execution
        if execution.get("auto_push", True):
            remote = execution.get("push_remote", "origin")
            branch = execution.get("push_branch", "main")
            pushed = await agent_git.push_head([repo_path], remote, branch, 0)
            if pushed:
                log.info("Pushed to %s/%s", remote, branch)
            else:
                log.warning("Push to %s/%s failed", remote, branch)

        # 3. Update review hash
        self._last_review_hash = cp.head_hash
        self._save_last_review_hash(cp.head_hash)

    # ── Phase: Reporting ─────────────────────────────────────────────────

    async def _report_results(self, result: Any) -> None:
        """Send execution results back to the Feishu group."""
        self._transition(PilotState.REPORTING)
        card = build_report_card(
            cycles_run=result.cycles_run,
            total_tool_calls=result.total_tool_calls,
            mission_status=result.mission_status,
            summary=result.summary[:2000],
        )
        await self._feishu.send_card(self._config.feishu.chat_id, card)
        log.info("Execution report sent to Feishu")

    # ══════════════════════════════════════════════════════════════════════
    #  Feishu event handlers
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_feishu_message(self, msg: FeishuMessage) -> None:
        """Handle an incoming Feishu text message based on current state."""
        if self._state == PilotState.DISCUSSING and self._discussion:
            await self._handle_discussion_message(msg)
        elif self._state in (PilotState.DIAGNOSING, PilotState.EXECUTING):
            await self._feishu.send_text(
                msg.chat_id,
                f"⏳ Currently {self._state.value}. Please wait.",
            )
        else:
            await self._feishu.send_text(
                msg.chat_id,
                "💤 No active proposal. Next check at "
                f"{self._config.schedule.hour:02d}:{self._config.schedule.minute:02d}.",
            )

    async def _handle_discussion_message(self, msg: FeishuMessage) -> None:
        """Process a message during the discussion phase."""
        text_lower = msg.text.lower().strip()

        # Check for text-based approval
        if text_lower in _APPROVAL_KEYWORDS:
            await self._feishu.send_text(
                msg.chat_id,
                "Detected approval keyword. Confirming: proceed with execution?  "
                "Reply 'yes' or click Approve on the card.",
            )
            return

        if text_lower in ("yes", "确认") and self._discussion:
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
            self._approval_event.set()
        elif action.action == "reject":
            self._rejection_event.set()
            await self._feishu.send_text(
                self._config.feishu.chat_id,
                "❌ Proposal rejected.",
            )
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

    def _resolve_workspace(self) -> Path:
        """Resolve the target project workspace path from diagnosis config."""
        return Path(self._config.diagnosis["harness"]["workspace"])

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
        """Path to the persistent pilot state file in the workspace."""
        return self._resolve_workspace() / _STATE_FILENAME

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

    def _load_last_review_hash(self) -> str:
        """Load the git hash from the last diagnosis run."""
        return self._load_state().get("last_review_hash", "")

    def _save_last_review_hash(self, head_hash: str) -> None:
        """Persist the git hash after a diagnosis run."""
        state = self._load_state()
        state["last_review_hash"] = head_hash
        self._save_state(state)

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
    def _is_no_action_proposal(proposal: str) -> bool:
        """Check if the proposal indicates no action is needed."""
        return _NO_ACTION_MARKER in proposal.lower()
