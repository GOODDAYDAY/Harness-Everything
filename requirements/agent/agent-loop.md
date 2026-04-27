# Agent Loop Orchestration

> Core autonomous agent runtime -- single LLM, full tool access, multi-cycle execution with persistent memory

Source: `harness/agent/agent_loop.py` (712 lines)

## Overview

`AgentLoop` is the main execution engine. A single LLM with every registered tool runs a connected tool-use dialogue for up to `max_tool_turns` calls per cycle. Between cycles, context persists in a file on disk (`agent_notes.md`) so the agent remembers where it left off. The loop terminates on mission-complete signal, mission-blocked signal, cycle exhaustion, or graceful shutdown.

## AgentConfig

Dataclass. Constructed via `__init__` or `from_dict(data)`. Carries all agent-mode-specific settings alongside the underlying `HarnessConfig`.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `harness` | `HarnessConfig` | (required) | Underlying LLM + workspace + tool settings |
| `mission` | `str` | `""` | The mission statement injected into system prompt |
| `max_cycles` | `int` | `999` | Hard cap on cycles. 999 = effectively unlimited |
| `continuous` | `bool` | `False` | When True, agent does not stop on "MISSION COMPLETE" -- keeps cycling until `max_cycles` or shutdown |
| `max_notes_cycles` | `int` | `30` | Number of most-recent cycle notes blocks kept in system prompt. Disk file keeps all. |
| `cycle_hooks` | `list[str]` | `["syntax", "static", "import_smoke"]` | Verification hooks to run after each cycle |
| `import_smoke_modules` | `list[str]` | `[]` | Modules for the import-smoke hook |
| `import_smoke_calls` | `list[str]` | `[]` | Callable expressions for import-smoke hook |
| `syntax_check_patterns` | `list[str]` | `["**/*.py"]` | Glob patterns for syntax checking |
| `auto_commit` | `bool` | `True` | Auto-commit after each successful cycle |
| `commit_repos` | `list[str]` | `["."]` | Repos to commit in (relative to workspace) |
| `auto_push` | `bool` | `False` | Push after successful commit |
| `auto_push_remote` | `str` | `"origin"` | Git remote for push |
| `auto_push_branch` | `str` | `"main"` | Git branch for push |
| `auto_tag_interval` | `int` | `0` | Create a tag every N successful cycles. 0 disables. |
| `auto_tag_prefix` | `str` | `"harness-r"` | Tag format: `<prefix>-<cycle_count>-<shortsha>` |
| `auto_tag_push` | `bool` | `True` | Push tags to remote |
| `pause_file` | `str` | `".harness.pause"` | Pause file path (relative to workspace or absolute) |
| `pause_poll_interval` | `int` | `30` | Seconds between pause-file checks |
| `auto_evaluate` | `bool` | `True` | Run DualEvaluator on each cycle's diff after commit |
| `meta_review_interval` | `int` | `5` | Run meta-review every N committed cycles. 0 disables. |
| `extra` | `dict[str, Any]` | `{}` | Project-specific parameters injected into system prompt as-is |
| `output_dir` | `str` | `"harness_output"` | Artifact root directory |
| `run_id` | `str \| None` | `None` | Optional run identifier |

### Validation (`__post_init__`)

- `max_cycles` must be >= 1
- `max_notes_cycles` must be >= 1
- `mission` must be a string (may be empty)

### Deserialization (`from_dict`)

- Strips keys starting with `"//"` or `"_"` (JSON-comment convention)
- Extracts `"harness"` key and constructs `HarnessConfig.from_dict()` from it
- Silently drops deprecated field `meta_review_inject` for backward compatibility
- Raises `ValueError` if `harness` key is missing or not a dict

## AgentResult

Dataclass. Final output of an agent run.

| Field | Type | Description |
|---|---|---|
| `success` | `bool` | True only when `mission_status == "complete"` |
| `cycles_run` | `int` | Number of cycles actually executed |
| `mission_status` | `str` | One of: `"complete"`, `"blocked"`, `"partial"`, `"exhausted"` |
| `total_tool_calls` | `int` | Sum of tool calls across all cycles |
| `summary` | `str` | Final text output (truncated to 4000 chars) |
| `run_dir` | `str` | Path to the artifact run directory |

## Mission Termination Signals

Matched case-insensitively as substrings in the agent's final text output:

- `"mission complete"` -- agent believes mission is done. Triggers `mission_status = "complete"`. Ignored when `continuous=True`.
- `"mission blocked"` -- agent hit something requiring human intervention. Triggers `mission_status = "blocked"`. Always respected, even in continuous mode.

## System Prompt Construction

### Base System Prompt (`_AGENT_BASE_SYSTEM`)

Static prompt establishing the agent as an autonomous software engineer. Core rules:

1. ONE THING PER CYCLE -- pick a single focused task, finish it, commit
2. Read before write -- use `cross_reference` before changing signatures
3. BATCH TOOL CALLS -- pack independent reads/searches into one response
4. Save findings to scratchpad IMMEDIATELY (at least 3 notes per cycle)
5. Focus persistence -- START each cycle by reading previous notes
6. Verify changes -- `lint_check` after edits, `test_runner` when tests exist
7. Use `context_budget` to check remaining turns

Includes quality feedback section explaining the 8-dimension auto-evaluation and strategic direction reviews.

### Completion Rules

Two variants, selected by `config.continuous`:

- **Oneshot** (`_COMPLETION_RULES_ONESHOT`): Agent should output "MISSION COMPLETE: ..." when done, "MISSION BLOCKED: ..." when stuck, or a status update to continue.
- **Continuous** (`_COMPLETION_RULES_CONTINUOUS`): Agent should NOT declare complete. Instead, explore for new improvements. Only "MISSION BLOCKED" terminates.

### `_build_system(cycle)` Assembly Order

1. Base system prompt (with appropriate completion rules)
2. Strategic direction from last meta-review (`## Strategic Direction`) -- injected before mission so agent reads direction first
3. Mission statement (`## MISSION`)
4. Project parameters from `config.extra` (`## Project Parameters`) -- rendered as `- **key**: value` lines
5. Persistent notes from previous cycles (`## Persistent Notes (previous cycles)`)
6. Current cycle indicator (`## Current Cycle` -- "Cycle N of up to M")

### `_build_cycle_user_message(cycle)` Content

- **Cycle 0**: "Begin the mission. Read enough of the codebase to understand what exists, then pick the first concrete task and execute it. Use the scratchpad tool to save any finding you'll need later."
- **Cycle > 0 (continuous)**: "Begin cycle N. Review your persistent notes above for what you did last cycle, then pick the next concrete task and execute it."
- **Cycle > 0 (oneshot)**: Same as continuous, plus "If the mission is complete, say so explicitly."

## AgentLoop Initialization

`__init__(self, config: AgentConfig)`:

1. Applies log level from `config.harness`
2. Logs startup banner
3. Creates `LLM` instance from config
4. Builds tool `registry` via `build_registry(allowed_tools, extra_tools)`
5. Checks for resumable run via `ArtifactStore.find_resumable(config.output_dir)`. If found, resumes; otherwise creates new `ArtifactStore`
6. Sets `_notes_path` to `<run_dir>/agent_notes.md`
7. Resolves `_repo_paths` via `agent_git.resolve_repo_paths(workspace, commit_repos)`
8. Creates `DualEvaluator(self.llm)` if `auto_evaluate` is True, else `None`
9. Initializes `_score_history: list[dict]` as empty
10. Initializes `_meta_review_context: str` as empty and `_last_review_hash: str` as empty
11. Installs signal handlers

## Signal Handling

- `_install_signal_handlers()` calls `install_shutdown_handlers(self._request_shutdown)` from `harness.core.signal_util`
- `_request_shutdown()` sets `_shutdown_requested = True` (idempotent, logs only on first call)
- Handles SIGINT and SIGTERM on the running event loop (no-op on Windows)

## Pause File Gate

- `_pause_file_path()` resolves `config.pause_file` relative to workspace (or as absolute if already absolute)
- `_check_pause(cycle)` blocks between cycles when the pause file exists:
  - Logs "pause file detected" with path and cycle number
  - Polls every `pause_poll_interval` seconds via `asyncio.sleep`
  - Exits early if `_shutdown_requested` becomes True during pause
  - Logs "pause file removed -- resuming" when file disappears
- Usage: `touch .harness.pause` to pause, `rm .harness.pause` to resume

## Persistent Notes (Scratchpad)

- File: `<run_dir>/agent_notes.md`
- `_read_notes()`: Reads the file, splits on `## Cycle N` markers (regex: `(?=^## Cycle \d+)`), keeps last `max_notes_cycles` blocks. Returns concatenated text.
- `_append_notes(cycle, summary)`: Appends a block with UTC ISO timestamp: `## Cycle N Summary (timestamp)\n<summary>`. Creates parent dirs if needed. Logs warning on `OSError`.

## Verification Hooks

### `_build_hooks()` -> `list[VerificationHook]`

Maps `config.cycle_hooks` names to hook instances:

| Name | Hook Class | Condition | Args |
|---|---|---|---|
| `"syntax"` | `SyntaxCheckHook` | Always when present | `config.syntax_check_patterns` |
| `"static"` | `StaticCheckHook` | Always when present | (none) |
| `"import_smoke"` | `ImportSmokeHook` | Only when `import_smoke_modules` or `import_smoke_calls` is non-empty | `modules=...`, `smoke_calls=...` |

### `_run_hooks(cycle, exec_log)` -> `list[str]`

Returns a list of hook-failure reasons (empty = all passed).

- Context passed to each hook: `{"cycle": cycle, "files_changed": collect_changed_paths(exec_log)}`
- If a hook crashes:
  - If `gates_commit` is True: adds to failures as `"<name>: crash (<exc>)"`
  - If `gates_commit` is False: logs error but does NOT add to failures
- If a hook returns `result.passed == False` and `gates_commit` is True: adds to failures with `result.errors` truncated to 200 chars
- All hooks log their name, passed status, and output (truncated to 120 chars)

## Main Loop (`run()`)

Returns `AgentResult`. Six phases per cycle:

### Phase 1: Execute

- Builds system prompt via `_build_system(cycle)`
- Builds user message via `_build_cycle_user_message(cycle)`
- Calls `self.llm.call_with_tools(messages, registry, system=system, max_turns=config.harness.max_tool_turns)`
- On exception: logs error with traceback, sets `mission_status = "blocked"`, breaks
- Tracks `total_tool_calls` across cycles
- Collects `changed_paths` via `collect_changed_paths(exec_log)`

### Phase 2: Verify

- Runs `_run_hooks(cycle, exec_log)` to get `hook_failures`

### Phase 3: Stage

- If `auto_commit` is True AND no hook failures: calls `agent_git.stage_changes(repo_paths, changed_paths)`
- If staging fails: logs warning, skips commit
- If hook failures exist: logs warning with failure details (truncated to 300 chars), skips staging and commit

### Phase 4: Evaluate

**4a. Cycle metrics** (always):
- Calls `collect_cycle_metrics(cycle=cycles_run, exec_log, changed_paths, hook_failures, elapsed_s)`
- Persists via `persist_cycle_metrics(cycle_m, artifacts.write, f"cycle_{cycles_run}")`
- Formats one-line summary via `format_metrics_summary(cycle_m)`
- On exception: logs warning, continues

**4b. Auto-evaluation** (when `auto_evaluate` is True):
- If staged and changed_paths: evaluates `git diff --cached` (staged diff)
- If no code changes: evaluates the agent's text output
- Calls `agent_eval.run_evaluation(evaluator, cycle, eval_input, mission, has_diff=...)`
- On success: records score, persists eval scores, formats eval notes and one-liner

**4c. Hooks summary line**:
- "all passed" or "FAILED: <failures>" (truncated to 200 chars)

### Phase 5: Commit

- If staged:
  - Builds commit message via `agent_git.build_commit_message(cycle, text, changed_paths, workspace, metrics_line=, eval_line=, hooks_line=)`
  - Commits via `agent_git.commit_staged(repo_paths, cycle, commit_msg)`
  - If committed and `auto_push`: pushes via `agent_git.push_head(repo_paths, remote, branch, cycle)`
  - Tags via `agent_git.tag_cycle(repo_paths, cycle, interval, prefix, push_remote, push_tag)`

**5b. Meta-review** (every `meta_review_interval` cycles, i.e., `cycles_run % interval == 0`; runs AFTER commit, not during Phase 4):
- Calls `agent_eval.run_meta_review(llm, cycle, score_history, last_review_hash, read_notes(), primary_repo, artifacts.write)`
- Updates `_meta_review_context` and `_last_review_hash` from result

### Phase 5c: Persist

- `_persist_cycle(cycle, text, exec_log, hook_failures)`:
  - Writes `output.txt` to `cycle_N/output.txt`
  - Writes `tool_log.json` to `cycle_N/tool_log.json` (JSON with indent=2)
  - If hook failures: writes `hook_failures.txt` to `cycle_N/hook_failures.txt`
- Extracts cycle summary via `_extract_cycle_summary(text, exec_log, hook_failures)`:
  - Takes last 500 chars of agent text (prefixed with "..." if truncated)
  - Counts tool usage: `name x count` for top 10 tools sorted by frequency
  - Prepends hook failures if any
- Prepends metrics line to summary
- Appends eval notes to summary
- Appends combined summary to `agent_notes.md`

### Phase 6: Control

1. If NOT continuous AND text contains "mission complete": `mission_status = "complete"`, break
2. If text contains "mission blocked": `mission_status = "blocked"`, break
3. If `_shutdown_requested`: `mission_status = "partial"`, break
4. Otherwise: deletes cycle variables, runs `gc.collect()`, checks pause file
5. If shutdown requested during pause: `mission_status = "partial"`, break

### Post-Loop

- Writes `final_summary.md` via `artifacts.write_final_summary()` with:
  - `mission_status`, `cycles_run`, `total_tool_calls`
  - Separator `---`
  - Full final summary text
- Returns `AgentResult` with `summary` truncated to 4000 chars

## Artifact Layout (per cycle)

```
<run_dir>/
  agent_notes.md
  cycle_1/
    output.txt
    tool_log.json
    hook_failures.txt      (only if hooks failed)
    metrics.json
    metrics_report.md
    eval_scores.json       (only if auto_evaluate)
    meta_review.md         (only at meta_review_interval)
  cycle_2/
    ...
  final_summary.md
```
