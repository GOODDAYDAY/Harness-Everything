# Agent Loop -- Requirements

The agent loop is the core orchestration engine. It runs an autonomous LLM through repeated cycles of work against a codebase until the mission is complete, blocked, or externally stopped.

---

## R-LOOP-01: Cycle lifecycle phases

Each cycle must proceed through a fixed sequence of phases: **Execute, Verify, Stage, Evaluate, Commit, Persist, Control**. No phase may be skipped (though a phase may be a no-op when its preconditions are not met -- e.g., Commit is skipped when hooks fail). The ordering guarantees that verification happens before committing, evaluation happens before persisting notes, and control-flow decisions happen last.

**Why:** Without a fixed phase order, it becomes possible to commit unverified code, persist notes that omit evaluation feedback, or check for mission-complete before the cycle's artifacts are saved. Each of these has happened in earlier versions and caused data loss or corrupted agent state.

**Acceptance criteria:**
- A cycle that changes files but fails a verification hook produces no git commit but still persists its artifacts and notes.
- A cycle that commits successfully has its evaluation scores included in the commit message.
- A cycle that declares MISSION COMPLETE has all its artifacts and notes flushed to disk before the loop terminates.

---

## R-LOOP-02: Graceful shutdown on interrupt

When the framework receives SIGINT or SIGTERM, it must set a shutdown flag and allow the **current cycle to complete all remaining phases** before exiting. The agent must not be killed mid-tool-call. The shutdown flag is checked only at the Control phase boundary (between cycles), not inside a phase.

**Why:** The agent runs on remote servers where deploy workflows restart it between chunks. Abrupt termination during a tool call can corrupt files on disk, leave git in a dirty state, or lose the current cycle's evaluation data. Additionally, on platforms where async signal handlers are not supported (Windows), the framework must degrade gracefully to default KeyboardInterrupt behavior.

**Acceptance criteria:**
- After receiving SIGINT during cycle N's Execute phase, the framework completes all of cycle N's remaining phases (Verify through Persist) before exiting.
- The final `AgentResult` has `mission_status = "partial"` and accurate `cycles_run` / `total_tool_calls` counts.
- A final summary artifact is written so that the run is not treated as resumable on next startup.

---

## R-LOOP-03: Run resumption

When starting up, the framework must detect whether a previous run in the same output directory was interrupted without writing a final summary. If such a run exists, the framework must resume into that run directory rather than creating a new one.

**Why:** Without resumption detection, each restart creates a new run directory, fragmenting artifacts across directories and making it impossible to see a continuous history. The agent's persistent notes would also reset, losing cross-cycle context.

**Acceptance criteria:**
- An interrupted run (no `final_summary` file) is detected and resumed on next startup. Log output indicates "resuming run: <path>".
- A completed run (final summary exists) is not resumed; a new run directory is created instead.

---

## R-LOOP-04: Persistent notes and cross-cycle memory

The agent's persistent notes file (`agent_notes.md`) must survive across cycles and restarts. Each cycle appends a summary block. The system prompt injects only the most recent N blocks (configurable) to prevent unbounded prompt growth, but the file on disk retains the complete history.

**Why:** The LLM has no memory between cycles -- each cycle starts a fresh conversation. Without persistent notes, the agent repeats work, forgets architectural decisions, and cannot see quality trends. The windowing (most recent N) prevents the system prompt from growing without bound, which would increase cost and eventually exceed context limits.

**Acceptance criteria:**
- After 40 cycles with `max_notes_cycles = 30`, the system prompt contains notes from cycles 11-40, but the file on disk contains all 40 cycle blocks.
- Each notes block includes a UTC timestamp, tool usage summary, hook status, and the agent's own output tail.
- If evaluation is enabled, the notes block also includes evaluation scores and critique excerpts.
- Notes append failures are logged but do not crash the cycle.

---

## R-LOOP-05: Mission completion and blocking signals

The agent terminates the loop by including specific signal phrases in its final text output. Two signals are recognized: one indicating the mission is done, and one indicating the agent is stuck and needs human help. In continuous mode, the "done" signal is ignored -- the agent keeps cycling.

**Why:** The loop needs a structured way to stop. Without it, the agent either runs forever (wasting resources) or requires the operator to manually kill it every time. The "blocked" signal prevents the agent from spinning endlessly on a problem it cannot solve (missing credentials, external system access, architectural decisions requiring human judgment).

**Acceptance criteria:**
- An agent that outputs the completion signal in non-continuous mode exits with `mission_status = "complete"`.
- An agent that outputs the completion signal in continuous mode continues to the next cycle.
- An agent that outputs the blocked signal exits with `mission_status = "blocked"` regardless of continuous mode.
- Signal matching is case-insensitive and substring-based, so minor wording variations (e.g., "Mission Complete!" vs "mission complete.") are both recognized.

---

## R-LOOP-06: Pause and resume via filesystem

The operator must be able to pause the agent between cycles by creating a designated file in the workspace, and resume it by removing that file. While paused, the agent must not start new cycles but must remain responsive to shutdown signals.

**Why:** Operators need a way to temporarily halt the agent without killing the process -- for example, to manually inspect the workspace, run their own tests, or wait for an external dependency. A filesystem-based mechanism works across SSH sessions and doesn't require a control API.

**Acceptance criteria:**
- Creating the pause file after cycle N causes the agent to complete cycle N, then block before starting cycle N+1.
- Removing the pause file causes the agent to resume from cycle N+1 within one poll interval.
- A shutdown signal received while paused causes immediate exit with `mission_status = "partial"`.
- The pause file path is configurable (absolute or relative to workspace).

---

## R-LOOP-07: Post-cycle verification hooks

After each cycle's tool execution, the framework must run a configurable set of verification hooks (syntax check, static analysis, import smoke test). Hook results gate the commit: if any gating hook fails, the cycle's changes are not committed. Non-gating hooks that crash are logged but do not block the commit.

**Why:** The LLM regularly introduces syntax errors, unused imports, or import-time crashes. Without gating hooks, these broken states get committed and compound -- the next cycle inherits a broken codebase and wastes its tool budget trying to recover. Hooks catch these before they enter the git history.

**Acceptance criteria:**
- A cycle that introduces a syntax error in a `.py` file is blocked from committing. The hook failure is recorded in the cycle's artifacts and notes.
- A cycle that passes all hooks proceeds to commit.
- A non-gating hook that throws an exception is logged at ERROR level but does not prevent the commit.
- Hook configuration supports enabling/disabling individual hooks by name.

---

## R-LOOP-08: System prompt construction

The system prompt must be assembled from multiple layers in a defined order: base instructions, strategic direction (from meta-review), mission statement, project parameters, persistent notes (windowed), and current cycle number. Each layer is optional and omitted when empty.

**Why:** The agent's behavior is shaped entirely by its system prompt. The layering order matters: strategic direction before mission ensures the agent reads high-level guidance first; notes at the end provide recency context. If layers are misordered or missing, the agent may ignore its mission, fail to follow strategic adjustments, or lose awareness of what cycle it's on.

**Acceptance criteria:**
- A cycle with meta-review context active includes a "Strategic Direction" section before the "MISSION" section in the system prompt.
- A cycle with project-specific `extra` parameters includes them as a "Project Parameters" section.
- The system prompt for cycle 1 in non-continuous mode includes instructions about signaling MISSION COMPLETE. In continuous mode, it instead instructs the agent to keep finding improvements.

---

## R-LOOP-09: Continuous vs. one-shot mode

The framework must support two operating modes. In **one-shot mode**, the agent works toward a defined mission and stops when it declares completion. In **continuous mode**, the agent cycles indefinitely (up to `max_cycles`), treating each cycle as an opportunity to find and fix the next highest-priority issue. The completion signal is ignored in continuous mode.

**Why:** Some workloads have a clear endpoint (implement feature X), while others are open-ended maintenance (keep improving test coverage, reduce tech debt). Continuous mode prevents the agent from prematurely declaring victory and stopping when there's more useful work to do.

**Acceptance criteria:**
- In one-shot mode, the agent receives instructions to signal MISSION COMPLETE when done.
- In continuous mode, the agent receives instructions to explore for new improvements rather than declaring completion.
- `max_cycles` is the hard cap in both modes.

---

## R-LOOP-10: Tool budget awareness

The agent must be told upfront how many tool turns it has per cycle, and the system prompt must instruct it to check its remaining budget and wrap up before running out. The framework enforces the tool turn limit on its side.

**Why:** Without budget awareness, the agent starts large refactoring tasks that it cannot finish within the cycle's tool limit. The unfinished work gets committed in a half-done state (or worse, the cycle's output is just "unknown (tool loop was cut off)"). Telling the agent about the budget lets it scope its work appropriately.

**Acceptance criteria:**
- The agent's system prompt includes the `context_budget` tool in its instruction set.
- The tool-use loop enforces `max_tool_turns` and stops the cycle when reached.
- When the tool loop is cut off, the commit message falls back to a diff-based summary instead of using the agent's (truncated) output.

---

## R-LOOP-11: Cycle artifact persistence

Every cycle must persist its artifacts to disk regardless of success or failure: the agent's text output, the full tool execution log (as JSON), and any hook failure details. These artifacts are written to a cycle-specific subdirectory within the run directory.

**Why:** Artifacts are the audit trail. Without them, there is no way to debug why a cycle went wrong, replay tool sequences, or analyze agent behavior at scale. They must persist even when the cycle crashes, because crash cycles are the most important ones to investigate.

**Acceptance criteria:**
- After every cycle, `output.txt` and `tool_log.json` exist in the cycle's artifact subdirectory.
- When hooks fail, `hook_failures.txt` also exists.
- Artifact write failures are logged but do not crash the cycle.

---

## R-LOOP-12: Memory pressure management

Between cycles, the framework must release references to the previous cycle's large data structures (tool log, text output, changed paths) and trigger garbage collection. This prevents memory growth proportional to the number of cycles in long-running sessions.

**Why:** The agent may run for hundreds of cycles in a single process. Each cycle's tool log can be megabytes. Without explicit cleanup, the process gradually consumes all available memory and gets OOM-killed.

**Acceptance criteria:**
- After cycle N completes and before cycle N+1 begins, the tool log and text output from cycle N are no longer reachable from the loop's scope.
- `gc.collect()` is called between cycles.

---

## R-LOOP-13: Smart squash integration

At configurable intervals, the framework must analyze recent commits, group related ones by logical task, and squash each group into a single clean commit. This only operates when auto-push is disabled (because squash rewrites history). The squash must be atomic: if it fails, the repository is left unchanged.

**Why:** The agent produces one commit per cycle, which creates a noisy git history where a single logical change (e.g., "add error handling to the API module") is spread across 5 separate commits. Squashing groups them into coherent units that are easier to review, cherry-pick, and revert.

**Acceptance criteria:**
- After every N committed cycles (configurable), the framework invokes the squash process.
- Squash is skipped when fewer than the minimum threshold of commits exist since the last squash.
- Squash is skipped when `auto_push` is enabled.
- A failed squash aborts the rebase cleanly and leaves HEAD unchanged. The cycle continues.
- After a successful squash, internal hash references (review baseline, squash baseline) are updated to reflect the rewritten history.
