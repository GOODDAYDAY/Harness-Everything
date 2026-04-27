"""PilotLoop — state machine orchestrating the daily improvement lifecycle.

Manages the full flow: schedule → diagnose → notify → discuss → execute → report.
Runs as a long-lived daemon process with Feishu WebSocket connectivity.
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
    AgentLoop (diagnosis and execution runs).
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
            # 1. Diagnose
            proposal, diagnostic_context = await self._run_diagnosis()

            # 2. Check if action is needed
            if self._is_no_action_proposal(proposal):
                await self._notify_no_action(proposal)
                return

            # 3. Notify operator via Feishu
            await self._notify_proposal(proposal)

            # 4. Discuss with operator (blocks until approved/rejected/expired)
            approved = await self._run_discussion(proposal, diagnostic_context)
            if not approved:
                return

            # 5. Execute the approved plan
            result = await self._run_execution()

            # 6. Report results
            await self._report_results(result)

        except Exception as exc:
            log.error("Improvement cycle failed: %s", exc, exc_info=True)
            await self._notify_error(str(exc))
        finally:
            self._transition(PilotState.IDLE)

    # ── Phase: Diagnosis ─────────────────────────────────────────────────

    async def _run_diagnosis(self) -> tuple[str, str]:
        """Run a harness agent in diagnosis mode and extract the proposal."""
        self._transition(PilotState.DIAGNOSING)

        # 1. Build and run the diagnosis agent
        agent_result = await self._execute_agent_run(
            self._config.build_diagnosis_agent_config()
        )

        # 2. Extract proposal from the last cycle output
        proposal = self._extract_proposal(agent_result)

        # 3. Collect diagnostic context from tool logs
        diagnostic_context = self._collect_diagnostic_context(agent_result)

        log.info(
            "Diagnosis complete, proposal_len=%d, context_len=%d, cycles=%d",
            len(proposal), len(diagnostic_context), agent_result.cycles_run,
        )
        return proposal, diagnostic_context

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
        result = await self._execute_agent_run(agent_config)

        log.info(
            "Execution complete, cycles=%d, tool_calls=%d, status=%s",
            result.cycles_run, result.total_tool_calls, result.mission_status,
        )
        return result

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

    async def _execute_agent_run(self, config_dict: dict[str, Any]) -> Any:
        """Create an AgentConfig and run AgentLoop programmatically."""
        from harness.agent.agent_loop import AgentConfig, AgentLoop

        agent_config = AgentConfig.from_dict(config_dict)
        loop = AgentLoop(agent_config)
        result = await loop.run()
        return result

    def _create_discussion_llm(self) -> Any:
        """Create a standalone LLM client for the discussion phase."""
        from harness.core.config import HarnessConfig
        from harness.core.llm import LLM

        harness_cfg = HarnessConfig.from_dict(
            self._config.build_discussion_harness_config()
        )
        return LLM(harness_cfg)

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

    # ── Data extraction ──────────────────────────────────────────────────

    @staticmethod
    def _extract_proposal(result: Any) -> str:
        """Extract the proposal text from the agent's final output."""
        if result.summary:
            return result.summary
        return "(No proposal generated — agent produced no output)"

    @staticmethod
    def _collect_diagnostic_context(result: Any) -> str:
        """Collect diagnostic data from the agent run's artifact directory.

        Reads tool_log.json files from each cycle to provide raw diagnostic
        data (DB query results, file contents) for the discussion LLM.
        """
        run_dir = Path(result.run_dir) if result.run_dir else None
        if not run_dir or not run_dir.exists():
            log.warning("No run_dir available for diagnostic context")
            return "(No diagnostic data available)"

        context_parts: list[str] = []
        for cycle_dir in sorted(run_dir.glob("cycle_*")):
            tool_log = cycle_dir / "tool_log.json"
            if tool_log.exists():
                try:
                    data = json.loads(tool_log.read_text())
                    for entry in data:
                        tool_name = entry.get("tool", "")
                        output = entry.get("output", "")
                        if tool_name and output:
                            context_parts.append(
                                f"### {tool_name}\n```\n{output[:3000]}\n```"
                            )
                except Exception as exc:
                    log.warning("Failed to read tool_log %s: %s", tool_log, exc)

        context = "\n\n".join(context_parts) if context_parts else "(No tool outputs found)"
        log.debug("Diagnostic context collected, parts=%d, total_len=%d", len(context_parts), len(context))
        return context

    @staticmethod
    def _is_no_action_proposal(proposal: str) -> bool:
        """Check if the proposal indicates no action is needed."""
        return _NO_ACTION_MARKER in proposal.lower()
