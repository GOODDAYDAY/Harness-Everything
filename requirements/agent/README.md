# Agent Domain

The agent domain covers the autonomous execution engine: a single LLM that runs multi-cycle missions against a codebase, automatically committing, evaluating, and adjusting its strategy over time.

## Scenarios

| File | Concern Area | Description |
|------|-------------|-------------|
| [agent-loop.md](agent-loop.md) | Cycle Lifecycle & Control | Cycle phases, shutdown, resumption, notes, mission control, mode selection, budget, artifacts, squash |
| [agent-eval.md](agent-eval.md) | Evaluation & Adaptation | Dual evaluation, mode adaptation, score persistence, meta-review, feedback loop |
| [agent-git.md](agent-git.md) | Version Control | Auto-commit, multi-repo, commit messages, staging, push, tagging, squash |

## Cycle Metrics

Cycle metrics measure how effectively the agent used its resources during a single cycle. They are computed from raw execution data and persisted as structured artifacts.

### US-01: As a cycle, I need my tool usage efficiency measured, so that the framework can detect wasteful patterns

After each cycle completes, the framework counts how many tool invocations succeeded on the first attempt, how many produced errors, and the ratio of reading operations to writing operations. This data reveals whether the agent is exploring productively or thrashing.

#### Acceptance Criteria
- Given a completed cycle with tool invocations, when metrics are collected, then the total count, error count, and first-try success rate are all recorded
- Given a cycle where the agent invoked both reading and writing tools, when metrics are collected, then the ratio of reads to writes is computed
- Given a cycle where some tool invocations failed, when metrics are collected, then each failure is counted toward the error total

### US-02: As a cycle, I need my output efficiency measured, so that the framework can assess whether tool effort translated into actual file changes

The number of files actually modified versus the number of tool calls made indicates whether the agent is being productive or spending effort without results.

#### Acceptance Criteria
- Given a cycle that modified three files using thirty tool calls, when metrics are collected, then the turns-per-change ratio reflects approximately ten calls per file
- Given a cycle with tool calls but no file changes, when metrics are collected, then the files-changed count is zero and the turns-per-change ratio indicates no productive output

### US-03: As a cycle, I need redundant file reads detected, so that the framework can identify wasted context window

When the agent reads the same file multiple times in a single cycle, it wastes context window capacity. Tracking redundant reads helps the agent learn to use its memory tools instead of re-reading.

#### Acceptance Criteria
- Given a cycle where the agent read the same file three times, when metrics are collected, then two redundant reads are recorded
- Given a cycle with no repeated file reads, when metrics are collected, then the redundant read count is zero

### US-04: As a cycle, I need my context targeting accuracy measured, so that the framework can evaluate whether reading effort was relevant to the work done

A high ratio of "files read that were also edited" to "total files read" means the agent targeted its exploration well. A low ratio means it read many files it never acted on.

#### Acceptance Criteria
- Given a cycle where the agent read five files and edited two of them, when metrics are collected, then the context hit rate is recorded as approximately forty percent
- Given a cycle where all files read were subsequently edited, when metrics are collected, then the context hit rate is one hundred percent and the waste rate is zero

### US-05: As a cycle, I need my workflow discipline assessed, so that the framework can verify the agent follows a read-then-write-then-test pattern

Good engineering discipline means gathering context before making changes (plan before act), consulting previous notes for continuity, and verifying work with tests after editing. These behavioral signals indicate process maturity.

#### Acceptance Criteria
- Given a cycle where the agent performed only read and search operations before its first file edit, when metrics are collected, then the plan-before-act signal is positive
- Given a cycle where the agent edited a file as its very first action, when metrics are collected, then the plan-before-act signal is negative
- Given a cycle where the agent ran tests after its last edit, when metrics are collected, then the test-after-edit signal is positive
- Given a cycle where the agent consulted its persistent notes, when metrics are collected, then the notes-consulted signal is positive

### US-06: As a cycle, I need my shell command reliance tracked, so that the framework can detect over-reliance on generic shell commands instead of purpose-built tools

The agent has specialized tools for file operations, search, and analysis. Excessive use of generic shell commands instead of these tools suggests the agent is bypassing the framework's safety and observability features.

#### Acceptance Criteria
- Given a cycle where more than half of all tool calls were shell commands, when metrics are collected, then the shell fraction exceeds fifty percent
- Given a cycle that used only specialized tools, when metrics are collected, then the shell fraction is zero

### US-07: As a cycle, I need my metrics persisted as structured data and a human-readable report, so that they can be reviewed later or consumed by other systems

Both a machine-readable format and a human-readable summary are needed: the structured format supports downstream analysis, while the report supports human review.

#### Acceptance Criteria
- Given completed cycle metrics, when they are persisted, then a structured data file and a formatted report file are both written to the cycle's artifact directory
- Given completed cycle metrics, when a one-line summary is generated, then it contains the key indicators (tool count, success rate, files changed, elapsed time, and workflow discipline signals)
