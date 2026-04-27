# Agent Domain

The agent domain implements Harness-Everything's autonomous execution engine: a single LLM that runs an open-ended tool-use dialogue against a codebase, committing verified changes, evaluating its own output, and adjusting strategy over time -- all without human intervention.

## Scope

This domain covers four concern areas:

| Concern | Document | Core question |
|---------|----------|---------------|
| Orchestration loop | [agent-loop.md](agent-loop.md) | How does the agent execute cycles, survive restarts, and know when to stop? |
| Evaluation | [agent-eval.md](agent-eval.md) | How does the framework measure quality and feed it back into the agent's behavior? |
| Git operations | [agent-git.md](agent-git.md) | How do code changes become commits, pushes, and tags without human git workflow? |

Cycle metrics are not a separate concern area. They are an internal observation mechanism that feeds data into evaluation and orchestration. They are described inline below and referenced where needed in the other documents.

## Key actors

- **Agent** -- the LLM instance executing tool calls within a cycle.
- **Framework** -- the surrounding Python runtime that manages cycles, hooks, git, and evaluation. The agent cannot override framework decisions (e.g., hook gating, commit policy).
- **Operator** -- the human who writes the mission, starts the run, and may pause/resume or send shutdown signals.

## Cycle metrics -- inline scenario

Cycle metrics exist to answer: "Was this cycle productive, or did the agent spin its wheels?" They are collected automatically after every cycle and serve two consumers: the evaluation system (which uses them as context) and the persistent notes (which let the agent self-correct in subsequent cycles).

### Scenario: Detecting a low-value cycle

**Context:** The agent has completed cycle 14. During this cycle it made 35 tool calls, changed 0 files, and read `config.py` four times.

**Expected behavior:**

1. The framework computes metrics from the raw tool execution log. No file I/O or LLM calls are involved in this computation -- it is purely deterministic.
2. The metrics report that `files_changed = 0`, `turns_per_change = 0.0`, `redundant_reads = 3` (same file read 4 times, 3 are redundant), and `plan_before_act = true` (all calls were reads).
3. The one-line summary is appended to the agent's persistent notes, e.g.: `[metrics] cycle=14 tools=35 err=0 success=100% files=0 turns/chg=0.0 bash=0% redundant=3 ctx_hit=0% notes=Y plan=Y test=N hooks=PASS elapsed=42s`.
4. The full metrics JSON and a formatted markdown report are written to the cycle's artifact directory.
5. On the next cycle, the agent sees the metrics line in its persistent notes and can recognize that cycle 14 was unproductive. The framework does not force a corrective action -- it trusts the agent to adjust.

### What the metrics measure (seven axes)

1. **Tool efficiency** -- total calls, error rate, read/write ratio, bash reliance, tool diversity.
2. **Output quality** -- files changed, tool calls per file change.
3. **Execution health** -- hook pass/fail, elapsed time, average tool duration.
4. **Redundancy** -- how many file reads were repeats of previously-read files.
5. **Behaviour signals** -- scratchpad usage (memory), test runner usage (verification), lint usage.
6. **Context quality** -- of the files the agent read, how many did it subsequently edit? A low hit rate means wasted context window.
7. **Memory and learning** -- did the agent consult its own notes? Did it read before writing? Did it test after editing?

### Design constraints on metrics

- Metrics computation must be **pure** -- it takes raw data (execution log, changed paths, hook results, elapsed time) and returns a result. No file I/O, no LLM calls, no imports from the orchestration loop.
- Metrics must never block or slow down the cycle. If computation fails, the cycle continues without metrics.
- The metrics schema is append-only in spirit: new axes can be added, but existing axis semantics must not change meaning across versions, because historical metric artifacts are used for trend analysis.
