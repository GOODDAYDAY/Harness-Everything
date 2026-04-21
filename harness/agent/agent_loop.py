"""AgentLoop — third runtime (fully autonomous single-LLM agent).

Unlike ``PipelineLoop`` (orchestrate → implement → review phases, multiple
LLM roles per round) or ``HarnessLoop`` (plan-and-execute one task),
``AgentLoop`` is a single LLM with every tool available, running a
connected tool-use dialogue for up to ``max_tool_turns`` calls per cycle.
Cross-cycle context lives in a persistent notes file on disk so the agent
can remember where it left off across restarts.

When to use which mode:
  * simple    — "fix this specific bug" — single call, commit, done
  * pipeline  — "iterate on quality" — scored rounds with roles
  * agent     — "maintain this codebase" — autonomous, open-ended, long-
                horizon; the agent itself decides what to work on and when
                to stop

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
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.core.artifacts import ArtifactStore
from harness.core.config import HarnessConfig
from harness.core.llm import LLM
from harness.core.signal_util import install_shutdown_handlers
from harness.pipeline.hooks import (
    ImportSmokeHook,
    StaticCheckHook,
    SyntaxCheckHook,
    VerificationHook,
)
from harness.tools import build_registry
from harness.tools.path_utils import collect_changed_paths

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
    settings (shared with simple / pipeline modes). The other fields are
    agent-mode-specific.
    """

    harness: HarnessConfig
    mission: str = ""
    # Hard cap on cycles. 999 is effectively "run until MISSION COMPLETE or
    # manual stop" — a 10-round pipeline chunk has no equivalent here.
    max_cycles: int = 999
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
    # If True, each cycle ends with `git add -A && git commit` in each
    # listed repo (relative to harness.workspace). Skipped when any
    # gating hook fails.
    auto_commit: bool = True
    commit_repos: list[str] = field(default_factory=lambda: ["."])
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
        # Strip comment-style keys (// or _ prefix) to match PipelineConfig's
        # JSON-comment convention so the same idiom works here.
        cleaned = {
            k: v for k, v in data.items()
            if not k.startswith("//") and not k.startswith("_")
        }
        harness_data = cleaned.pop("harness", None)
        if not isinstance(harness_data, dict):
            raise ValueError(
                "agent config requires a 'harness' object with LLM/workspace settings"
            )
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
You are an autonomous software engineer working on a codebase without
supervision for an extended session. Each turn you may call any tool in
the schema to read the repo, search, edit, run tests, etc. You are the
only agent here — there is no orchestrator, no evaluator, no next phase.

Guidelines:
  * Work against the MISSION stated below. Break it into tasks you pick
    yourself. Use the scratchpad tool to record findings that you'll
    need after many turns — conversation history gets pruned, scratchpad
    notes do not.
  * Prefer batch_read / batch_edit / batch_write over single-file tools;
    one LLM round-trip can do a lot.
  * Before you write, read the relevant code. Before you change a
    function signature, grep for callers.
  * Verify each change: py_compile after edits, run tests when tests
    exist. The post-cycle hook will catch syntax errors, but failing in
    your own loop is faster.
  * Commit discipline: each cycle ends with a commit of everything in
    the workspace that changed (auto). Make your changes in the cycle
    coherent.

Signalling the end of the mission:
  * Output "MISSION COMPLETE: <one-line summary>" when you believe the
    mission is done.
  * Output "MISSION BLOCKED: <what you need from a human>" when you hit
    something you cannot resolve autonomously (missing credentials,
    external-system access, an architectural decision that requires a
    product call, etc.).
  * Otherwise, end your turn with a brief status update and the loop
    will start a new cycle fresh.
"""


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
        self._install_signal_handlers()

    # ---- signal handling (mirrors PipelineLoop pattern) ----

    def _request_shutdown(self) -> None:
        if not self._shutdown_requested:
            self._shutdown_requested = True
            log.warning(
                "Agent: shutdown requested (signal) — finishing current cycle…"
            )

    def _install_signal_handlers(self) -> None:
        install_shutdown_handlers(self._request_shutdown)

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
        parts = [_AGENT_BASE_SYSTEM]
        if self.config.mission:
            parts.append(f"\n## MISSION\n{self.config.mission}\n")
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
                log.warning("Agent: hook %s crashed: %s", hook.name, exc)
                if getattr(hook, "gates_commit", False):
                    failures.append(f"{hook.name}: crash ({exc})")
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

    # ---- commit ----

    async def _auto_commit(
        self, cycle: int, agent_text: str, changed_paths: list[str],
    ) -> None:
        """Stage only the paths the agent touched, then commit in each repo.

        Using ``git add <path>`` instead of ``git add -A`` keeps unrelated
        untracked files (editor settings, local debug logs, external-reference
        docs, etc.) out of the commit. Empty changed_paths still produces an
        allow-empty commit so the cycle is recorded in the log.
        """
        workspace = Path(self.config.harness.workspace)
        summary_line = agent_text.strip().splitlines()[0][:80] if agent_text.strip() else ""
        msg = f"agent: cycle {cycle + 1}"
        if summary_line:
            msg += f" — {summary_line}"

        for repo in self.config.commit_repos:
            repo_path = workspace / repo if not Path(repo).is_absolute() else Path(repo)
            if not repo_path.is_dir():
                log.warning("Agent: commit_repos entry not found: %s", repo_path)
                continue
            try:
                if changed_paths:
                    # ``git add -- <paths>`` records deletions too when the
                    # path is gone, so delete_file / move_file sources
                    # correctly show up as removals in the commit.
                    add = await asyncio.create_subprocess_exec(
                        "git", "add", "--", *changed_paths,
                        cwd=str(repo_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, add_err = await add.communicate()
                    if add.returncode != 0:
                        log.warning(
                            "Agent: git add failed in %s: %s",
                            repo, add_err.decode(errors="replace")[:200],
                        )
                commit = await asyncio.create_subprocess_exec(
                    "git", "commit", "--allow-empty", "-m", msg,
                    cwd=str(repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await commit.communicate()
                if commit.returncode == 0:
                    log.info(
                        "Agent: committed cycle %d in %s (%d path(s))",
                        cycle + 1, repo, len(changed_paths),
                    )
                else:
                    log.warning(
                        "Agent: commit failed in %s: %s",
                        repo, stderr.decode(errors="replace")[:200],
                    )
            except Exception as exc:
                log.warning("Agent: commit error in %s: %s", repo, exc)

    # ---- main loop ----

    async def run(self) -> AgentResult:
        total_tool_calls = 0
        cycles_run = 0
        mission_status = "exhausted"
        final_summary = ""

        for cycle in range(self.config.max_cycles):
            cycles_run = cycle + 1
            log.info(
                "Agent cycle %d/%d starting",
                cycle + 1, self.config.max_cycles,
            )
            t_start = time.monotonic()

            # 1. build prompts
            system = self._build_system(cycle)
            user_msg = self._build_cycle_user_message(cycle)

            # 2. run tool-use loop
            try:
                text, exec_log = await self.llm.call_with_tools(
                    [{"role": "user", "content": user_msg}],
                    self.registry,
                    system=system,
                    max_turns=self.config.harness.max_tool_turns,
                )
            except Exception as exc:
                log.error(
                    "Agent cycle %d crashed: %s", cycle + 1, exc, exc_info=True,
                )
                final_summary = f"cycle {cycle + 1} crashed: {exc}"
                mission_status = "blocked"
                break

            total_tool_calls += len(exec_log)
            elapsed = time.monotonic() - t_start
            log.info(
                "Agent cycle %d: %d tool calls in %.1fs (total tool calls: %d)",
                cycle + 1, len(exec_log), elapsed, total_tool_calls,
            )

            # 3. post-cycle hooks
            hook_failures = await self._run_hooks(cycle, exec_log)

            # 4. auto-commit (only when all gating hooks passed). Stage the
            # exact paths the agent's tool log declares it touched, not
            # `git add -A` — unrelated untracked files (Claude Code settings,
            # run logs, external-reference docs) would otherwise sneak into
            # the commit.
            changed_paths = collect_changed_paths(exec_log)
            if self.config.auto_commit and not hook_failures:
                await self._auto_commit(cycle, text, changed_paths)
            elif hook_failures:
                log.warning(
                    "Agent: skipping commit for cycle %d — hook failures: %s",
                    cycle + 1, "; ".join(hook_failures)[:500],
                )

            # 5. persist cycle artifacts
            self._persist_cycle(cycle, text, exec_log, hook_failures)

            # 6. compute cycle summary (notes) and append
            summary = self._extract_cycle_summary(text, exec_log, hook_failures)
            self._append_notes(cycle, summary)

            # 7. check for mission-status signals
            lowered = (text or "").lower()
            if _MISSION_COMPLETE_MARKER in lowered:
                mission_status = "complete"
                final_summary = text
                log.info("Agent: MISSION COMPLETE signalled at cycle %d", cycle + 1)
                break
            if _MISSION_BLOCKED_MARKER in lowered:
                mission_status = "blocked"
                final_summary = text
                log.info("Agent: MISSION BLOCKED signalled at cycle %d", cycle + 1)
                break

            if self._shutdown_requested:
                mission_status = "partial"
                final_summary = text
                log.info(
                    "Agent: graceful shutdown after cycle %d", cycle + 1,
                )
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
