# Agent Loop

> Autonomous agent orchestration -- cycle management, evaluation, git operations, metrics

The agent domain replaces the old multi-phase pipeline with a single-LLM autonomous loop. One LLM instance runs a connected tool-use dialogue across multiple cycles, with persistent cross-cycle memory (scratchpad file on disk), automatic verification hooks, git operations, evaluation, and strategic meta-review.

## Module Map

| Module | Role |
|---|---|
| `agent/__init__.py` | Public API surface -- exports `AgentConfig`, `AgentLoop`, `AgentResult` |
| `agent/agent_loop.py` | Core orchestration: config, system prompt construction, cycle execution, hook dispatch, artifact persistence, control flow (mission signals, pause, shutdown) |
| `agent/agent_eval.py` | Evaluation wrapper: DualEvaluator integration, score tracking, formatting, periodic meta-review |
| `agent/agent_git.py` | Git operations: stage, commit, push, tag, diff queries, commit message construction |
| `agent/cycle_metrics.py` | Per-cycle metrics: 7-axis quality measurement, serialisation, reporting |

## Scenarios

| Scenario | Location | Status |
|---|---|---|
| Agent Loop Orchestration | [agent-loop.md](agent-loop.md) | Draft |
| Agent Evaluation | [agent-eval.md](agent-eval.md) | Draft |
| Agent Git Operations | [agent-git.md](agent-git.md) | Draft |
| Cycle Metrics | (inline below) | Draft |

## Cycle Metrics (inline scenario)

`cycle_metrics.py` computes per-cycle quality metrics across seven axes from raw execution data. It has no side effects and no imports from `agent_loop` (avoids circular deps). The agent loop passes raw data in; the module returns a `CycleMetrics` dataclass.

### Tool Classification

Two frozen sets classify tools for metric computation:

- `_READ_TOOLS` -- 23 tools that gather context without mutating: `batch_read`, `read_file`, `grep_search`, `glob_search`, `list_directory`, `tree`, `symbol_extractor`, `code_analysis`, `cross_reference`, `feature_search`, `project_map`, `file_info`, `call_graph`, `data_flow`, `dependency_analyzer`, `diff_files`, `context_budget`, `tool_discovery`, `todo_scan`, `git_status`, `git_diff`, `git_log`, `git_search`
- `_WRITE_TOOLS` -- 10 tools that mutate files: `batch_edit`, `batch_write`, `edit_file`, `write_file`, `file_patch`, `find_replace`, `delete_file`, `move_file`, `copy_file`, `ast_rename`

### CycleMetrics Dataclass

Fields grouped by axis:

**Axis 1 -- Tool Efficiency**
- `total_tool_calls: int` (default 0)
- `error_tool_calls: int` (default 0)
- `first_try_success_rate: float` (default 0.0) -- fraction of non-error calls
- `read_calls: int` (default 0)
- `write_calls: int` (default 0)
- `bash_calls: int` (default 0)
- `read_write_ratio: float` (default 0.0)
- `bash_fraction: float` (default 0.0)
- `unique_tools_used: int` (default 0)
- `tool_distribution: dict[str, int]` (default empty) -- sorted descending by count

**Axis 2 -- Output Quality**
- `files_changed: int` (default 0)
- `turns_per_change: float` (default 0.0) -- `total_tool_calls / files_changed`

**Axis 3 -- Execution Health**
- `hooks_passed: bool` (default True)
- `hook_failure_count: int` (default 0)
- `elapsed_s: float` (default 0.0)
- `avg_tool_duration_ms: float` (default 0.0)
- `total_output_chars: int` (default 0)

**Axis 4 -- Redundancy**
- `redundant_reads: int` (default 0) -- re-reads of the same file path
- `redundant_read_rate: float` (default 0.0)

**Axis 5 -- Behaviour Signals**
- `scratchpad_calls: int` (default 0)
- `test_runner_calls: int` (default 0)
- `lint_calls: int` (default 0)

**Axis 6 -- Context Quality**
- `context_files_read: int` (default 0) -- unique files explicitly read
- `context_files_used: int` (default 0) -- read files that were later edited
- `context_hit_rate: float` (default 0.0) -- `used / read`
- `context_waste_rate: float` (default 0.0) -- `1 - hit_rate`

**Axis 7 -- Memory & Learning**
- `notes_consulted: bool` (default False) -- agent read `agent_notes.md`
- `plan_before_act: bool` (default False) -- only reads/search/scratchpad before first write
- `test_after_edit: bool` (default False) -- `test_runner` ran after last edit
- `edit_test_cycles: int` (default 0) -- number of edit-then-test iteration pairs

### Pure Collector Functions

Each `_compute_*` function takes raw data and returns a metrics dict. No side effects.

| Function | Axis | Input | Logic |
|---|---|---|---|
| `_compute_tool_efficiency(exec_log)` | 1 + 3 + 5 | exec_log | Counts tool types against `_READ_TOOLS`/`_WRITE_TOOLS`, errors, bash, scratchpad, test_runner, lint_check. Computes `avg_tool_duration_ms` and `total_output_chars` (Axis 3) from entries. |
| `_compute_change_efficiency(exec_log, changed_paths)` | 2 | exec_log, changed_paths | `turns_per_change = len(exec_log) / len(changed_paths)` or 0 |
| `_compute_redundancy(exec_log)` | 4 | exec_log | Extracts paths from `batch_read` (`.paths[]`) and `read_file` (`.path`). Counts paths seen more than once. |
| `_compute_context_quality(exec_log, changed_paths)` | 6 | exec_log, changed_paths | Builds set of read paths (same extraction as redundancy), intersects with `changed_paths` set. |
| `_compute_memory_learning(exec_log)` | 7 | exec_log | Scans for `agent_notes` in read paths. Checks all calls before first write are in `_READ_TOOLS` or `scratchpad`/`context_budget`. Checks `test_runner` after last write. Counts edit-then-test pairs. |

### Public API

- `collect_cycle_metrics(cycle, exec_log, changed_paths, hook_failures, elapsed_s) -> CycleMetrics` -- the only function the agent loop calls. Rounds: `first_try_success_rate` to 4 decimals, `read_write_ratio` to 2, `bash_fraction` to 4, `turns_per_change` to 2, `elapsed_s` to 1, `avg_tool_duration_ms` to 0, redundancy/context rates to 4.
- `metrics_to_dict(m) -> dict` -- uses `dataclasses.asdict()`.
- `format_detailed_report(m) -> str` -- multi-line markdown report with sections for each axis, including a tool-distribution bar chart.
- `format_summary(m) -> str` -- one-line summary: `[metrics] cycle=N tools=N err=N success=N% files=N turns/chg=N.N bash=N% redundant=N ctx_hit=N% notes=Y/N plan=Y/N test=Y/N hooks=PASS/FAIL elapsed=Ns`.
- `persist_cycle_metrics(metrics, artifacts_write, cycle_segment)` -- writes `metrics.json` and `metrics_report.md` to cycle artifacts via passed-in `artifacts_write` callable.
