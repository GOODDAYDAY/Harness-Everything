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
    GameSmokeHook,
    GodotSyntaxHook,
    ImportSmokeHook,
    StaticCheckHook,
    SyntaxCheckHook,
    VerificationHook,
)
from harness.evaluation.dual_evaluator import DualEvaluator
from harness.tools import build_registry
from harness.tools.path_utils import collect_changed_paths, collect_read_paths
from harness.agent.coverage_tracker import CoverageTracker, collect_project_files
from harness.agent.cycle_metrics import (
    collect_cycle_metrics,
    format_summary as format_metrics_summary,
    persist_cycle_metrics,
)
from harness.agent import agent_git, agent_eval

log = logging.getLogger(__name__)


# Signals the agent emits in its final text to terminate the loop.
# Matched case-insensitively as substrings — generous by design so minor
# wording variations don't trap the loop forever.
_MISSION_COMPLETE_MARKER = "mission complete"
_MISSION_BLOCKED_MARKER = "mission blocked"

# Internal constant — no longer user-configurable.
_PAUSE_POLL_SECONDS = 30   # pause-file polling interval


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
    # Pause file — when this file exists in the workspace, the agent
    # finishes its current cycle and then sleeps until the file is removed.
    # Usage: `touch .harness.pause` to pause, `rm .harness.pause` to resume.
    pause_file: str = ".harness.pause"
    # ── Auto-evaluation ──
    # When True, the framework automatically runs DualEvaluator on each
    # cycle's git diff after commit.  Scores are logged and appended to
    # agent_notes.md so the agent sees quality trends without needing to
    # self-evaluate.
    auto_evaluate: bool = True
    # ── Periodic checkpoint ──
    # The checkpoint is the sole periodic orchestration point: strategic
    # review + maintenance actions (squash, tag).  Every
    # ``meta_review_interval`` cycles, the framework runs a checkpoint
    # that analyses score trends + git history and produces strategic
    # direction guidance.  Also runs once at startup ("cold checkpoint")
    # to orient the agent based on previous notes / git history.
    # Set to 0 to disable periodic checkpoints (startup still runs).
    meta_review_interval: int = 5
    # ── Checkpoint feature toggles ──
    # At each checkpoint, the framework may also perform maintenance
    # actions.  These are boolean switches — the LLM decides the details
    # (e.g. which commits to group, whether there are enough to squash).
    auto_squash: bool = False   # LLM groups recent commits and squashes
    auto_tag: bool = False      # tag HEAD at each checkpoint
    auto_tag_prefix: str = "harness-r"
    auto_tag_push: bool = True
    # ── Game testing ──
    # Master switch for AI-driven game testing capabilities (game tools,
    # game hooks, GameBridge process launching).  Default OFF — these
    # features give the agent the ability to launch processes, open TCP
    # connections, inject input, and capture screenshots.  Enable only
    # when the agent is being used for game development with Godot.
    enable_game_testing: bool = False
    # Project-specific parameters.  The framework does not interpret these —
    # they are injected into the system prompt as-is so the agent can see
    # project-level context (e.g. coding conventions, domain glossary,
    # focus areas, forbidden patterns).  Keys and values should be strings
    # or simple types that serialise to readable text.
    extra: dict[str, Any] = field(default_factory=dict)
    # Artifact root — a new run_id subdirectory is created under this.
    output_dir: str = "harness_output"
    run_id: str | None = None
    # When True, the skills system is active: skills are discovered from
    # <workspace>/.harness/skills/ and injected into the system prompt.
    # Set False to bypass the skill system entirely (legacy behaviour).
    enable_skills: bool = True

    def __post_init__(self) -> None:
        if self.max_cycles < 1:
            raise ValueError(f"max_cycles must be >= 1, got {self.max_cycles}")
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
        for deprecated in (
            "meta_review_inject", "auto_squash_interval", "auto_tag_interval",
            "squash_min_commits", "max_notes_cycles", "pause_poll_interval",
        ):
            cleaned.pop(deprecated, None)
        cleaned["harness"] = HarnessConfig.from_dict(harness_data)
        return cls(**cleaned)


@dataclass
class AgentResult:
    """Final output of an agent run."""

    success: bool
    cycles_run: int
    mission_status: str  # "complete" | "blocked" | "partial" | "exhausted" | "stopped"
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

        # Filter out game tools when game testing is disabled
        extra = list(config.harness.extra_tools or [])
        if not config.enable_game_testing:
            _GAME_TOOLS = {"game_launch", "game_screenshot", "game_input", "game_state"}
            before = len(extra)
            extra = [t for t in extra if t not in _GAME_TOOLS]
            if before != len(extra):
                log.info("Agent: game testing disabled — stripped %d game tool(s) from extra_tools", before - len(extra))

        self.registry = build_registry(
            config.harness.allowed_tools or None,
            extra_tools=extra or None,
            custom_tools_path=config.harness.custom_tools_path or None,
        )

        # ── Skills system ──
        self._skill_registry = None
        if config.enable_skills:
            from harness.skills import build_skill_registry
            from harness.skills.skill_lookup import SkillLookupTool
            from harness.skills.skill_update import UpdateSkillTool

            self._skill_registry = build_skill_registry(
                config.harness.workspace,
                mission=config.mission,
            )
            if self._skill_registry:
                self.registry.register(SkillLookupTool())
                self.registry.register(UpdateSkillTool())
                config.harness.skill_registry = self._skill_registry  # type: ignore[attr-defined]
                log.info(
                    "Agent: skills loaded — %d total (%d auto-load, %d on-demand)",
                    len(self._skill_registry),
                    len(self._skill_registry.auto_load_skills()),
                    len(self._skill_registry.on_demand_skills()),
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

        # Checkpoint state
        self._meta_review_context: str = ""
        self._last_review_hash: str = ""
        self._coverage_tracker = CoverageTracker()

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
        ``_PAUSE_POLL_SECONDS`` seconds and honours shutdown signals.
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
            await asyncio.sleep(_PAUSE_POLL_SECONDS)

        log.info("Agent: pause file removed — resuming from cycle %d.", cycle + 2)

    # ---- per-cycle prompt construction ----

    def _read_notes(self) -> str:
        """Load the persistent cycle notes.

        The file contains a mix of compressed history (from checkpoint
        LLM compression) and recent per-cycle summaries.  Return the
        full contents — compression keeps the file size manageable.
        """
        if not self._notes_path.exists():
            return ""
        try:
            return self._notes_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

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

        # ── Skills OR legacy mission injection ──
        if self._skill_registry and self._skill_registry:
            from harness.skills.resolver import resolve_cycle_skills

            auto_text, index_text = resolve_cycle_skills(self._skill_registry)
            if auto_text:
                parts.append(f"\n{auto_text}")
            if index_text:
                parts.append(f"\n{index_text}")
        elif self.config.mission:
            # Legacy fallback: no skills, use raw mission string.
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
        # Game-related hooks — only when game testing is enabled
        if self.config.enable_game_testing:
            if "godot_syntax" in names:
                game_path = self.config.extra.get("game_path") or os.environ.get("HARNESS_GAME_PATH")
                godot_path = self.config.extra.get("godot_path") or os.environ.get("HARNESS_GODOT_PATH")
                hooks.append(GodotSyntaxHook(game_path=game_path, godot_path=godot_path))
            if "game_smoke" in names:
                game_path = self.config.extra.get("game_path") or os.environ.get("HARNESS_GAME_PATH")
                godot_path = self.config.extra.get("godot_path") or os.environ.get("HARNESS_GODOT_PATH")
                hooks.append(GameSmokeHook(game_path=game_path, godot_path=godot_path))
        elif "godot_syntax" in names or "game_smoke" in names:
            log.info("Agent: game testing disabled — skipping game hooks")
        return hooks

    async def _run_hooks(
        self, cycle: int, exec_log: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Run post-cycle hooks and return (failures, hook_summaries).

        ``failures``: list of failure reason strings (empty = all passed).
        ``hook_summaries``: list of dicts with hook name, passed, output,
        and errors — used to inject runtime feedback into evaluator input.
        """
        hooks = self._build_hooks()
        if not hooks:
            return [], []
        ctx = {
            "cycle": cycle,
            "files_changed": collect_changed_paths(exec_log),
        }
        # Run all hooks in parallel — they are independent read-only checks.
        results = await asyncio.gather(
            *(hook.run(self.config.harness, ctx) for hook in hooks),
            return_exceptions=True,
        )
        failures: list[str] = []
        hook_summaries: list[dict[str, Any]] = []
        for hook, result in zip(hooks, results):
            if isinstance(result, Exception):
                if getattr(hook, "gates_commit", False):
                    log.warning("Agent: hook %s crashed: %s", hook.name, result)
                    failures.append(f"{hook.name}: crash ({result})")
                else:
                    log.error("Agent: hook %s crashed (non-gating, not blocking commit): %s", hook.name, result)
                hook_summaries.append({
                    "hook": hook.name, "passed": False,
                    "output": "", "errors": f"crash: {result}",
                })
                continue
            if not result.passed and getattr(hook, "gates_commit", False):
                failures.append(
                    f"{hook.name}: {(result.errors or 'failed')[:200]}"
                )
            hook_summaries.append({
                "hook": hook.name, "passed": result.passed,
                "output": (result.output or "")[:500],
                "errors": (result.errors or "")[:500],
            })
            log.info(
                "Agent: hook %s: passed=%s output=%s",
                hook.name, result.passed, (result.output or "")[:120],
            )
        return failures, hook_summaries

    # ---- main loop ----

    async def run(self) -> AgentResult:
        total_tool_calls = 0
        cycles_run = 0
        mission_status = "exhausted"
        final_summary = ""
        workspace = Path(self.config.harness.workspace)
        primary_repo = self._repo_paths[0] if self._repo_paths else workspace

        # ── Startup checkpoint (cold analysis) ──
        # Same function as the periodic checkpoint, but skips maintenance
        # actions (no squash/tag).  Gives the agent strategic direction from
        # cycle 1 instead of a blind "go read the codebase".
        self._last_review_hash = await agent_git.get_head_hash(primary_repo)
        notes = self._read_notes()
        if notes or self._score_history:
            cp = await agent_eval.run_checkpoint(
                self.llm, -1, self._score_history,
                self._last_review_hash, notes,
                primary_repo, self.artifacts.write,
                notes_path=self._notes_path,
            )
            self._meta_review_context = cp.meta_context
            self._last_review_hash = cp.head_hash
            log.info("Agent: startup checkpoint complete, direction set")

        for cycle in range(self.config.max_cycles):
            cycles_run = cycle + 1
            log.info("Agent cycle %d/%d starting", cycles_run, self.config.max_cycles)
            t_start = time.monotonic()

            # Record HEAD before the tool loop so we can diff against it
            # even if the agent commits during its own tool calls.
            cycle_start_hash = await agent_git.get_head_hash(primary_repo)

            # ── Phase 1: Execute ──
            system = self._build_system(cycle)
            user_msg = self._build_cycle_user_message(cycle)
            try:
                text, exec_log, llm_calls, conversation, raw_conversation = await self.llm.call_with_tools(
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
            read_paths = collect_read_paths(exec_log)
            self._coverage_tracker.update(read_paths, changed_paths)
            log.info(
                "Agent cycle %d: %d tool calls in %.1fs",
                cycles_run, len(exec_log), elapsed,
            )

            # ── Phase 2: Verify ──
            hook_failures, hook_summaries = await self._run_hooks(cycle, exec_log)

            # ── Phase 3: Stage ──
            staged = False
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
                    # Fallback: agent may have committed during its tool loop,
                    # leaving the staging area empty.  Use committed diff instead.
                    if not eval_input.strip() and cycle_start_hash:
                        log.info(
                            "Agent: staged diff empty but changes expected — "
                            "falling back to committed diff since %s",
                            cycle_start_hash,
                        )
                        eval_input = await agent_git.get_committed_diff(
                            primary_repo, cycle_start_hash,
                        )
                else:
                    eval_input = text or ""

                # Inject runtime feedback from hooks into evaluator input
                if hook_summaries:
                    runtime_lines = ["\n\n--- RUNTIME FEEDBACK ---"]
                    for hs in hook_summaries:
                        status = "PASS" if hs["passed"] else "FAIL"
                        runtime_lines.append(f"\n[{hs['hook']}] {status}")
                        if hs["output"]:
                            runtime_lines.append(hs["output"])
                        if hs["errors"]:
                            runtime_lines.append(f"Errors: {hs['errors']}")
                    eval_input += "\n".join(runtime_lines)

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
                else:
                    log.warning("Agent: commit failed for cycle %d", cycles_run)

            # ── Checkpoint (every N cycles) ──
            # Unified orchestration point: strategic review + squash + tag.
            # Meta-review and squash LLM calls run in parallel; execution
            # actions (squash → tag) run sequentially with dependencies.
            interval = self.config.meta_review_interval
            if interval > 0 and cycles_run % interval == 0:
                # Compute coverage report for meta-review (US-11)
                coverage_report = ""
                try:
                    project_files = collect_project_files(
                        self.config.harness.workspace,
                    )
                    cov = self._coverage_tracker.report(project_files)
                    coverage_report = CoverageTracker.format_report(cov)
                except Exception as exc:
                    log.warning("Agent: coverage report failed: %s", exc)

                cp = await agent_eval.run_checkpoint(
                    self.llm, cycle, self._score_history,
                    self._last_review_hash, self._read_notes(),
                    primary_repo, self.artifacts.write,
                    notes_path=self._notes_path,
                    auto_squash=self.config.auto_squash and not self.config.auto_push,
                    auto_tag=self.config.auto_tag,
                    tag_prefix=self.config.auto_tag_prefix,
                    tag_push=self.config.auto_tag_push,
                    push_remote=self.config.auto_push_remote,
                    coverage_report=coverage_report,
                )
                self._meta_review_context = cp.meta_context
                self._last_review_hash = cp.head_hash

                # Enforce MetaReview decision (US-12)
                if cp.decision is not None:
                    if cp.decision.action == "stop":
                        log.info(
                            "Agent: MetaReview STOP: %s", cp.decision.reason,
                        )
                        mission_status = "stopped"
                        final_summary = (
                            f"MetaReview stopped the agent: {cp.decision.reason}"
                        )
                        break
                    elif cp.decision.action == "pivot":
                        log.info(
                            "Agent: MetaReview PIVOT: %s → %s",
                            cp.decision.reason,
                            cp.decision.pivot_direction,
                        )
                        pivot_block = (
                            f"**PIVOT DIRECTIVE**: {cp.decision.pivot_direction}\n"
                            f"Reason: {cp.decision.reason}\n\n"
                        )
                        self._meta_review_context = (
                            pivot_block + self._meta_review_context
                        )

            # ── Phase 6: Persist ──
            self._persist_cycle(
                cycle, text, exec_log, hook_failures,
                system=system,
                llm_calls=llm_calls, conversation=conversation,
                raw_conversation=raw_conversation,
            )
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
            summary=final_summary[:16000],
            run_dir=str(self.artifacts.run_dir),
        )

    # ---- artifact persistence ----

    def _persist_cycle(
        self,
        cycle: int,
        text: str,
        exec_log: list[dict[str, Any]],
        hook_failures: list[str],
        *,
        system: str = "",
        llm_calls: list[dict[str, Any]] | None = None,
        conversation: list[dict[str, Any]] | None = None,
        raw_conversation: list[dict[str, Any]] | None = None,
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
            if system:
                self.artifacts.write(system, seg, "system_prompt.txt")
            if llm_calls is not None:
                self.artifacts.write(
                    json.dumps(llm_calls, indent=2, default=str),
                    seg, "llm_calls.json",
                )
            if conversation is not None:
                self.artifacts.write(
                    json.dumps(conversation, indent=2, default=str),
                    seg, "conversation.json",
                )
            if raw_conversation is not None:
                self.artifacts.write(
                    json.dumps(raw_conversation, indent=2, default=str),
                    seg, "conversation_raw.json",
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
