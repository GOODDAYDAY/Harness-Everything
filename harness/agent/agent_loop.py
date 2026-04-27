"""AgentLoop — fully autonomous single-LLM agent runtime.

``AgentLoop`` is a single LLM with every tool available, running a
connected tool-use dialogue for up to ``max_tool_turns`` calls per cycle.
Cross-cycle context lives in a persistent notes file on disk so the agent
can remember where it left off across restarts.

Control flow per cycle:
  1. Build system prompt = mission + persistent notes from disk
  2. One ``call_with_tools`` loop, tools = full registry, up to
     ``max_tool_turns`` tool calls
  3. Post-cycle hooks (syntax / static / import smoke)
  4. If hooks pass AND ``auto_commit`` is set: ``git add -A && git commit``
  5. Append cycle artifacts (output.txt, tool_log.json) + update
     ``agent_notes.md``
  6. Check for MISSION COMPLETE / MISSION BLOCKED signals in the agent's
     final text; otherwise loop
"""

from __future__ import annotations

import asyncio
import datetime
import gc
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.core.artifacts import ArtifactStore
from harness.core.config import HarnessConfig
from harness.core.llm import LLM
from harness.core.signal_util import install_shutdown_handlers
from harness.core.hooks import (
    ImportSmokeHook,
    StaticCheckHook,
    SyntaxCheckHook,
    VerificationHook,
)
from harness.evaluation.dual_evaluator import DualEvaluator
from harness.tools import build_registry
from harness.tools.path_utils import collect_changed_paths
from harness.agent.cycle_metrics import (
    collect_cycle_metrics,
    format_summary as format_metrics_summary,
    persist_cycle_metrics,
)
from harness.agent import agent_git, agent_eval, agent_squash

log = logging.getLogger(__name__)


# Signals the agent emits in its final text to terminate the loop.
# Matched case-insensitively as substrings — generous by design so minor
# wording variations don't trap the loop forever.
_MISSION_COMPLETE_MARKER = "mission complete"
_MISSION_BLOCKED_MARKER = "mission blocked"


@dataclass
class AgentConfig:
    """Configuration for the autonomous agent loop.

    The ``harness`` field carries the underlying LLM + workspace + tool
    settings. The other fields are agent-mode-specific.
    """

    harness: HarnessConfig
    mission: str = ""
    # Hard cap on cycles. 999 is effectively "run until MISSION COMPLETE or
    # manual stop".
    max_cycles: int = 999
    # When True, the agent does not stop on "MISSION COMPLETE" — it keeps
    # cycling until max_cycles or graceful shutdown.  Designed for standing
    # maintenance missions where there is no logical endpoint.
    continuous: bool = False
    # Number of most-recent cycle notes blocks to keep in the system-prompt
    # injection. The file on disk keeps everything; this only controls what
    # the LLM sees to avoid unbounded prompt growth.
    max_notes_cycles: int = 30
    # Verification hooks that run after each cycle's tool loop. Names match
    # the switches in ``_build_hooks``; "syntax" / "static" / "import_smoke"
    # are recognised today.
    cycle_hooks: list[str] = field(
        default_factory=lambda: ["syntax", "static", "import_smoke"]
    )
    # Optional list of modules the import-smoke hook exercises.
    import_smoke_modules: list[str] = field(default_factory=list)
    import_smoke_calls: list[str] = field(default_factory=list)
    # Syntax-check glob patterns (passed to SyntaxCheckHook).
    syntax_check_patterns: list[str] = field(
        default_factory=lambda: ["**/*.py"]
    )
    # If True, each cycle ends with `git add <changed paths> && git commit`
    # in each listed repo (relative to harness.workspace). Skipped when any
    # gating hook fails.
    auto_commit: bool = True
    commit_repos: list[str] = field(default_factory=lambda: ["."])
    # After a successful commit, push the branch to origin (or whichever
    # remote is configured). Keep False for offline / sandboxed dev.
    auto_push: bool = False
    auto_push_remote: str = "origin"
    auto_push_branch: str = "main"
    # Every N successful cycles, create a tag + push it. Cycle tags can
    # trigger deploy workflows. 0 disables tagging entirely.
    # Tag format: <prefix>-<cycle_count>-<shortsha>, e.g. harness-r-10-a3f5d2c.
    auto_tag_interval: int = 0
    auto_tag_prefix: str = "harness-r"
    auto_tag_push: bool = True
    # Pause file — when this file exists in the workspace, the agent
    # finishes its current cycle and then sleeps until the file is removed.
    # Usage: `touch .harness.pause` to pause, `rm .harness.pause` to resume.
    pause_file: str = ".harness.pause"
    # How often (seconds) to check whether the pause file has been removed.
    pause_poll_interval: int = 30
    # ── V5 auto-evaluation ──
    # When True, the framework automatically runs DualEvaluator on each
    # cycle's git diff after commit.  Scores are logged and appended to
    # agent_notes.md so the agent sees quality trends without needing to
    # self-evaluate.
    auto_evaluate: bool = True
    # ── V5 periodic meta-review ──
    # Every ``meta_review_interval`` committed cycles, the framework runs
    # a meta-review LLM call that analyses score trends + git history and
    # produces strategic direction guidance, injected into subsequent
    # cycles' system prompts.  Set to 0 to disable.
    meta_review_interval: int = 5
    # ── Smart squash ──
    # Every ``auto_squash_interval`` cycles, the framework asks the LLM to
    # group recent commits by logical task and squash each group into a
    # single clean commit.  Only runs when ``auto_push`` is False (squash
    # rewrites history).  Set to 0 to disable.
    auto_squash_interval: int = 0
    # Minimum number of commits since last squash before triggering.
    squash_min_commits: int = 3
    # Project-specific parameters.  The framework does not interpret these —
    # they are injected into the system prompt as-is so the agent can see
    # project-level context (e.g. coding conventions, domain glossary,
    # focus areas, forbidden patterns).  Keys and values should be strings
    # or simple types that serialise to readable text.
    extra: dict[str, Any] = field(default_factory=dict)
    # Artifact root — a new run_id subdirectory is created under this.
    output_dir: str = "harness_output"
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.max_cycles < 1:
            raise ValueError(f"max_cycles must be >= 1, got {self.max_cycles}")
        if self.max_notes_cycles < 1:
            raise ValueError(
                f"max_notes_cycles must be >= 1, got {self.max_notes_cycles}"
            )
        if not isinstance(self.mission, str):
            raise ValueError("mission must be a string (may be empty)")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentConfig":
        # Strip comment-style keys (// or _ prefix) — JSON-comment convention.
        cleaned = {
            k: v for k, v in data.items()
            if not k.startswith("//") and not k.startswith("_")
        }
        harness_data = cleaned.pop("harness", None)
        if not isinstance(harness_data, dict):
            raise ValueError(
                "agent config requires a 'harness' object with LLM/workspace settings"
            )
        # Silently drop deprecated fields for backward compatibility.
        cleaned.pop("meta_review_inject", None)
        cleaned["harness"] = HarnessConfig.from_dict(harness_data)
        return cls(**cleaned)


@dataclass
class AgentResult:
    """Final output of an agent run."""

    success: bool
    cycles_run: int
    mission_status: str  # "complete" | "blocked" | "partial" | "exhausted"
    total_tool_calls: int
    summary: str
    run_dir: str = ""


_AGENT_BASE_SYSTEM = """\
You are an autonomous software engineer working on a codebase for an
extended session. Each turn you call tools to read, search, edit, and
test. You are the only agent — no orchestrator, no next phase.

Core rules:
  * ONE THING PER CYCLE. Pick a single focused task, finish it, commit.
  * Read before you write. Use cross_reference before changing signatures.
  * BATCH TOOL CALLS. Pack independent reads/searches into one response.
    Prefer batch_read / batch_edit over single-file tools. Never use bash
    to read files — use batch_read, grep_search, symbol_extractor, etc.
  * Save key findings to scratchpad IMMEDIATELY — conversation history
    gets pruned, scratchpad survives. Save at least 3 notes per cycle.
  * Focus persistence: START each cycle by reading your previous cycle's
    notes. Execute the planned next target unless there is a concrete
    reason to switch.
  * Verify your changes: lint_check after edits, test_runner when tests
    exist. Unused imports (F401) will block your commit.
  * Use context_budget to check remaining turns. Wrap up before running out.

Quality feedback:
  Your work is automatically evaluated after each committed cycle on 8
  dimensions (correctness, completeness, specificity, architecture fit,
  caller impact, maintenance debt, emergent behaviour, rollback safety).
  Scores and critiques appear in your persistent notes — review them to
  understand what the framework values. Every few cycles, a strategic
  direction review analyses your score trends and adjusts priorities.

Signalling the end of the mission:
{completion_rules}\
"""

_COMPLETION_RULES_ONESHOT = """\
  * Output "MISSION COMPLETE: <one-line summary>" when you believe the
    mission is done.
  * Output "MISSION BLOCKED: <what you need from a human>" when you hit
    something you cannot resolve autonomously (missing credentials,
    external-system access, an architectural decision that requires a
    product call, etc.).
  * Otherwise, end your turn with a brief status update and the loop
    will start a new cycle fresh."""

_COMPLETION_RULES_CONTINUOUS = """\
  * At the end of this cycle, summarise what you fixed and what remains.
    The next cycle will continue automatically — focus on the highest-priority
    remaining issue each time.
  * If you believe the current mission direction is fully addressed, do NOT
    just declare it complete. Instead, explore the codebase for new high-value
    improvements: run tests, scan for TODOs, review recent git history,
    check for missing error handling or test coverage.
  * Output "MISSION BLOCKED: <what you need from a human>" when you hit
    something you cannot resolve autonomously.
  * Otherwise, end your turn with a brief status update and the loop
    will start a new cycle fresh."""


class AgentLoop:
    """Run an autonomous agent against a mission until complete / blocked / exhausted.

    Designed to be crash-safe: artifacts persist to disk between cycles
    so a SIGKILL won't lose progress that's already been committed to git
    or flushed to ``agent_notes.md``.
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        config.harness.apply_log_level()
        log.info(config.harness.startup_banner())

        self.llm = LLM(config.harness)
        self.registry = build_registry(
            config.harness.allowed_tools or None,
            extra_tools=config.harness.extra_tools or None,
        )

        # Resume if there's an incomplete run directory; otherwise new run.
        existing = ArtifactStore.find_resumable(config.output_dir)
        if existing:
            self.artifacts = existing
            log.info("Agent: resuming run: %s", self.artifacts.run_dir)
        else:
            self.artifacts = ArtifactStore(config.output_dir, config.run_id)
            log.info("Agent: new run: %s", self.artifacts.run_dir)

        self._notes_path = Path(self.artifacts.run_dir) / "agent_notes.md"
        self._shutdown_requested: bool = False

        # Resolved repo paths for git operations (computed once).
        self._repo_paths = agent_git.resolve_repo_paths(
            config.harness.workspace, config.commit_repos,
        )

        # V5: auto-evaluation state
        self._evaluator = DualEvaluator(self.llm) if config.auto_evaluate else None
        self._score_history: list[dict[str, Any]] = []

        # V5: meta-review state
        self._meta_review_context: str = ""
        self._last_review_hash: str = ""
        # Smart squash state
        self._last_squash_hash: str = ""

        self._install_signal_handlers()

    # ---- signal handling ----

    def _request_shutdown(self) -> None:
        if not self._shutdown_requested:
            self._shutdown_requested = True
            log.warning(
                "Agent: shutdown requested (signal) — finishing current cycle…"
            )

    def _install_signal_handlers(self) -> None:
        install_shutdown_handlers(self._request_shutdown)

    # ---- pause-file gate ----

    def _pause_file_path(self) -> Path:
        """Resolve the pause file path relative to workspace."""
        p = self.config.pause_file
        if os.path.isabs(p):
            return Path(p)
        return Path(self.config.harness.workspace) / p

    async def _check_pause(self, cycle: int) -> None:
        """Block until the pause file is removed (if it exists).

        Called between cycles. While paused, checks every
        ``pause_poll_interval`` seconds and honours shutdown signals.
        """
        pf = self._pause_file_path()
        if not pf.exists():
            return

        log.info(
            "Agent: pause file detected (%s) after cycle %d — pausing. "
            "Remove the file to resume.",
            pf, cycle + 1,
        )
        while pf.exists():
            if self._shutdown_requested:
                log.info("Agent: shutdown requested while paused — exiting.")
                return
            await asyncio.sleep(self.config.pause_poll_interval)

        log.info("Agent: pause file removed — resuming from cycle %d.", cycle + 2)

    # ---- per-cycle prompt construction ----

    def _read_notes(self) -> str:
        """Load the persistent cycle notes, trimmed to ``max_notes_cycles``."""
        if not self._notes_path.exists():
            return ""
        try:
            raw = self._notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        # Split on the cycle marker and keep the last N blocks. Each block
        # begins with ``## Cycle N Summary``. We keep the most recent so the
        # LLM sees how the work has most recently been progressing.
        parts = re.split(r"(?=^## Cycle \d+)", raw, flags=re.MULTILINE)
        parts = [p for p in parts if p.strip()]
        kept = parts[-self.config.max_notes_cycles :]
        return "".join(kept).strip()

    def _append_notes(self, cycle: int, summary: str) -> None:
        """Append this cycle's summary to ``agent_notes.md``."""
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        block = (
            f"\n## Cycle {cycle + 1} Summary ({ts})\n"
            f"{summary.strip()}\n"
        )
        try:
            self._notes_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._notes_path, "a", encoding="utf-8") as f:
                f.write(block)
        except OSError as exc:
            log.warning("Agent: failed to append notes: %s", exc)

    def _build_system(self, cycle: int) -> str:
        completion_rules = (
            _COMPLETION_RULES_CONTINUOUS if self.config.continuous
            else _COMPLETION_RULES_ONESHOT
        )
        base = _AGENT_BASE_SYSTEM.format(completion_rules=completion_rules)
        parts = [base]

        # Strategic direction from the last meta-review (before mission,
        # so the agent reads direction first).
        if self._meta_review_context:
            parts.append(
                f"\n## Strategic Direction\n{self._meta_review_context}\n"
            )

        if self.config.mission:
            parts.append(f"\n## MISSION\n{self.config.mission}\n")
        if self.config.extra:
            lines = "\n".join(f"- **{k}**: {v}" for k, v in self.config.extra.items())
            parts.append(f"\n## Project Parameters\n{lines}\n")
        notes = self._read_notes()
        if notes:
            parts.append(f"\n## Persistent Notes (previous cycles)\n{notes}\n")
        parts.append(
            f"\n## Current Cycle\nCycle {cycle + 1} of up to "
            f"{self.config.max_cycles}.\n"
        )
        return "".join(parts)

    def _build_cycle_user_message(self, cycle: int) -> str:
        if cycle == 0:
            return (
                "Begin the mission. Read enough of the codebase to understand "
                "what exists, then pick the first concrete task and execute it. "
                "Use the scratchpad tool to save any finding you'll need later."
            )
        if self.config.continuous:
            return (
                f"Begin cycle {cycle + 1}. Review your persistent notes above for "
                "what you did last cycle, then pick the next concrete task and "
                "execute it."
            )
        return (
            f"Begin cycle {cycle + 1}. Review your persistent notes above for "
            "what you did last cycle, then pick the next concrete task and "
            "execute it. If the mission is complete, say so explicitly."
        )

    # ---- hooks (post-cycle verification) ----

    def _build_hooks(self) -> list[VerificationHook]:
        hooks: list[VerificationHook] = []
        names = set(self.config.cycle_hooks)
        if "syntax" in names:
            hooks.append(SyntaxCheckHook(self.config.syntax_check_patterns))
        if "static" in names:
            hooks.append(StaticCheckHook())
        if "import_smoke" in names and (
            self.config.import_smoke_modules or self.config.import_smoke_calls
        ):
            hooks.append(
                ImportSmokeHook(
                    modules=self.config.import_smoke_modules or None,
                    smoke_calls=self.config.import_smoke_calls or None,
                )
            )
        return hooks

    async def _run_hooks(
        self, cycle: int, exec_log: list[dict[str, Any]],
    ) -> list[str]:
        """Return a list of hook-failure reasons (empty = all passed)."""
        hooks = self._build_hooks()
        if not hooks:
            return []
        ctx = {
            "cycle": cycle,
            "files_changed": collect_changed_paths(exec_log),
        }
        failures: list[str] = []
        for hook in hooks:
            try:
                result = await hook.run(self.config.harness, ctx)
            except Exception as exc:
                if getattr(hook, "gates_commit", False):
                    log.warning("Agent: hook %s crashed: %s", hook.name, exc)
                    failures.append(f"{hook.name}: crash ({exc})")
                else:
                    log.error("Agent: hook %s crashed (non-gating, not blocking commit): %s", hook.name, exc)
                continue
            if not result.passed and getattr(hook, "gates_commit", False):
                failures.append(
                    f"{hook.name}: {(result.errors or 'failed')[:200]}"
                )
            log.info(
                "Agent: hook %s: passed=%s output=%s",
                hook.name, result.passed, (result.output or "")[:120],
            )
        return failures

    # ---- main loop ----

    async def run(self) -> AgentResult:
        total_tool_calls = 0
        cycles_run = 0
        mission_status = "exhausted"
        final_summary = ""

        # Initialize squash baseline to current HEAD
        if self.config.auto_squash_interval > 0 and self._repo_paths:
            self._last_squash_hash = await agent_git.get_head_hash(
                self._repo_paths[0],
            )

        for cycle in range(self.config.max_cycles):
            cycles_run = cycle + 1
            log.info("Agent cycle %d/%d starting", cycles_run, self.config.max_cycles)
            t_start = time.monotonic()

            # ── Phase 1: Execute ──
            system = self._build_system(cycle)
            user_msg = self._build_cycle_user_message(cycle)
            try:
                text, exec_log = await self.llm.call_with_tools(
                    [{"role": "user", "content": user_msg}],
                    self.registry,
                    system=system,
                    max_turns=self.config.harness.max_tool_turns,
                )
            except Exception as exc:
                log.error("Agent cycle %d crashed: %s", cycles_run, exc, exc_info=True)
                final_summary = f"cycle {cycles_run} crashed: {exc}"
                mission_status = "blocked"
                break

            total_tool_calls += len(exec_log)
            elapsed = time.monotonic() - t_start
            changed_paths = collect_changed_paths(exec_log)
            log.info(
                "Agent cycle %d: %d tool calls in %.1fs",
                cycles_run, len(exec_log), elapsed,
            )

            # ── Phase 2: Verify ──
            hook_failures = await self._run_hooks(cycle, exec_log)

            # ── Phase 3: Stage ──
            staged = False
            workspace = Path(self.config.harness.workspace)
            primary_repo = self._repo_paths[0] if self._repo_paths else workspace
            if self.config.auto_commit and not hook_failures:
                staged = await agent_git.stage_changes(self._repo_paths, changed_paths)
                if not staged:
                    log.warning("Agent: staging failed for cycle %d, skipping commit", cycles_run)
            elif hook_failures:
                log.warning(
                    "Agent: skipping commit (cycle %d) — %s",
                    cycles_run, "; ".join(hook_failures)[:300],
                )

            # ── Phase 4: Evaluate ──
            # 4a. Cycle metrics (always)
            metrics_line = ""
            try:
                cycle_m = collect_cycle_metrics(
                    cycle=cycles_run,
                    exec_log=exec_log,
                    changed_paths=changed_paths,
                    hook_failures=hook_failures,
                    elapsed_s=elapsed,
                )
                persist_cycle_metrics(
                    cycle_m, self.artifacts.write, f"cycle_{cycles_run}",
                )
                metrics_line = format_metrics_summary(cycle_m)
            except Exception as exc:
                log.warning("Agent: metrics failed cycle %d: %s", cycles_run, exc)

            # 4b. Auto-evaluation (every cycle)
            eval_notes = ""
            eval_line = ""
            if self.config.auto_evaluate:
                if staged and changed_paths:
                    eval_input = await agent_git.get_staged_diff(primary_repo)
                else:
                    # No code changes — evaluate the agent's reasoning output
                    eval_input = text or ""
                eval_score = await agent_eval.run_evaluation(
                    self._evaluator, cycle, eval_input, self.config.mission,
                    has_diff=bool(staged and changed_paths),
                )
                if eval_score is not None:
                    agent_eval.record_score(eval_score, cycle, self._score_history)
                    agent_eval.persist_eval_scores(
                        eval_score, cycle, self.artifacts.write,
                    )
                    eval_notes = agent_eval.format_eval_notes(eval_score)
                    eval_line = agent_eval.format_eval_oneliner(eval_score)

            # 4c. Hooks summary line
            if not hook_failures:
                hooks_line = "all passed"
            else:
                hooks_line = "FAILED: " + "; ".join(hook_failures)[:200]

            # ── Phase 5: Commit ──
            committed = False
            if staged:
                commit_msg = await agent_git.build_commit_message(
                    cycle, text, changed_paths, workspace,
                    metrics_line=metrics_line,
                    eval_line=eval_line,
                    hooks_line=hooks_line,
                )
                commit_ok = await agent_git.commit_staged(
                    self._repo_paths, cycle, commit_msg,
                )
                if commit_ok:
                    committed = True
                    if self.config.auto_push:
                        push_ok = await agent_git.push_head(
                            self._repo_paths,
                            self.config.auto_push_remote,
                            self.config.auto_push_branch,
                            cycle,
                        )
                        if not push_ok:
                            log.warning("Agent: push failed for cycle %d", cycles_run)
                    await agent_git.tag_cycle(
                        self._repo_paths,
                        cycle,
                        self.config.auto_tag_interval,
                        self.config.auto_tag_prefix,
                        self.config.auto_push_remote,
                        self.config.auto_tag_push,
                    )
                else:
                    log.warning("Agent: commit failed for cycle %d", cycles_run)

            # 4d. Meta-review (every N cycles)
            interval = self.config.meta_review_interval
            if interval > 0 and cycles_run % interval == 0:
                result = await agent_eval.run_meta_review(
                    self.llm, cycle, self._score_history,
                    self._last_review_hash, self._read_notes(),
                    primary_repo, self.artifacts.write,
                )
                self._meta_review_context = result.context
                self._last_review_hash = result.head_hash

            # 4e. Smart squash (every N cycles, after meta-review)
            squash_interval = self.config.auto_squash_interval
            if (squash_interval > 0
                    and cycles_run % squash_interval == 0
                    and self.config.auto_commit
                    and not self.config.auto_push):
                new_hash = await agent_squash.run_squash(
                    self.llm, primary_repo,
                    self._last_squash_hash,
                    min_commits=self.config.squash_min_commits,
                )
                self._last_squash_hash = new_hash
                # Squash rewrites history — update review hash too
                if new_hash != self._last_review_hash:
                    self._last_review_hash = new_hash

            # ── Phase 5: Persist ──
            self._persist_cycle(cycle, text, exec_log, hook_failures)
            summary = self._extract_cycle_summary(text, exec_log, hook_failures)
            if metrics_line:
                summary = f"{metrics_line}\n{summary}"
            if eval_notes:
                summary += f"\n{eval_notes}"
            self._append_notes(cycle, summary)

            # ── Phase 6: Control ──
            lowered = (text or "").lower()
            if not self.config.continuous and _MISSION_COMPLETE_MARKER in lowered:
                mission_status = "complete"
                final_summary = text
                log.info("Agent: MISSION COMPLETE at cycle %d", cycles_run)
                break
            if _MISSION_BLOCKED_MARKER in lowered:
                mission_status = "blocked"
                final_summary = text
                log.info("Agent: MISSION BLOCKED at cycle %d", cycles_run)
                break
            if self._shutdown_requested:
                mission_status = "partial"
                final_summary = text
                log.info("Agent: graceful shutdown after cycle %d", cycles_run)
                break

            final_summary = text
            del text, exec_log, hook_failures, changed_paths, summary, eval_notes
            gc.collect()

            await self._check_pause(cycle)
            if self._shutdown_requested:
                mission_status = "partial"
                log.info("Agent: shutdown during pause after cycle %d", cycles_run)
                break

        # Write the final summary so find_resumable doesn't treat the run as
        # still-open next time.
        try:
            self.artifacts.write_final_summary(
                f"mission_status: {mission_status}\n"
                f"cycles_run: {cycles_run}\n"
                f"total_tool_calls: {total_tool_calls}\n\n"
                f"---\n\n{final_summary}\n"
            )
        except Exception as exc:
            log.warning("Agent: failed to write final_summary: %s", exc)

        return AgentResult(
            success=(mission_status == "complete"),
            cycles_run=cycles_run,
            mission_status=mission_status,
            total_tool_calls=total_tool_calls,
            summary=final_summary[:4000],
            run_dir=str(self.artifacts.run_dir),
        )

    # ---- artifact persistence ----

    def _persist_cycle(
        self,
        cycle: int,
        text: str,
        exec_log: list[dict[str, Any]],
        hook_failures: list[str],
    ) -> None:
        seg = f"cycle_{cycle + 1}"
        try:
            self.artifacts.write(text or "", seg, "output.txt")
            self.artifacts.write(
                json.dumps(exec_log, indent=2, default=str),
                seg, "tool_log.json",
            )
            if hook_failures:
                self.artifacts.write(
                    "\n".join(hook_failures), seg, "hook_failures.txt",
                )
        except Exception as exc:
            log.warning("Agent: failed to persist cycle %d: %s", cycle + 1, exc)

    def _extract_cycle_summary(
        self,
        text: str,
        exec_log: list[dict[str, Any]],
        hook_failures: list[str],
    ) -> str:
        """Build a compact summary to append to agent_notes.md."""
        # Take the last 500 chars of the agent's text output — usually where
        # the agent puts its own conclusions. Fall back to tool stats.
        tail = (text or "").strip()
        if len(tail) > 500:
            tail = "…" + tail[-500:]
        tools_used: dict[str, int] = {}
        for e in exec_log:
            tools_used[e["tool"]] = tools_used.get(e["tool"], 0) + 1
        tool_line = ", ".join(
            f"{name}×{n}" for name, n in sorted(
                tools_used.items(), key=lambda kv: -kv[1],
            )[:10]
        )
        parts = [
            f"Tool usage: {tool_line or '(none)'}  total={len(exec_log)}",
        ]
        if hook_failures:
            parts.append(f"HOOK FAILURES: {'; '.join(hook_failures)[:300]}")
        if tail:
            parts.append(f"Output tail: {tail}")
        return "\n".join(parts)
