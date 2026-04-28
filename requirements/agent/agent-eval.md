# Agent Evaluation

User stories for dual evaluation, mode adaptation, score persistence, meta-review, and feedback loop.

---

## Dual Evaluation

### US-01: As the evaluator, I need to score each cycle's deliverable using two independent evaluation perspectives, so that the quality assessment is robust against single-evaluator blind spots

Each cycle's output is evaluated by two isolated evaluators that score independently. The dual perspective reduces the chance that a single evaluator's bias produces misleading quality signals. The individual scores are combined into a composite score.

#### Acceptance Criteria
- Given a cycle produced code changes, when evaluation runs, then two independent scores are produced
- Given two independent scores are produced, when they are combined, then a composite score is computed from both
- Given only one evaluator succeeds and the other fails, when the result is assembled, then the available score is still recorded

### US-02: As the evaluator, I need to evaluate code changes differently from reasoning-only output, so that cycles without commits are still assessed on a relevant scale

Not every cycle produces code changes -- some cycles are pure exploration or planning. The evaluator adapts its criteria based on whether the cycle produced a diff or only reasoning output. This prevents penalizing legitimate exploration cycles for lacking code changes.

#### Acceptance Criteria
- Given a cycle produced staged code changes, when evaluation runs, then the evaluator uses implementation-focused criteria
- Given a cycle produced no code changes, when evaluation runs, then the evaluator uses reasoning-focused criteria
- Given a cycle produced an empty output, when evaluation runs, then a placeholder description is used instead of empty input

---

## Score Persistence

### US-03: As the evaluator, I need each cycle's evaluation scores and critiques persisted as a structured artifact, so that they can be reviewed later or consumed by other systems

The detailed scores, including individual evaluator scores and their textual critiques, are written to the cycle's artifact directory. This supports post-run analysis and debugging of quality trends.

#### Acceptance Criteria
- Given evaluation produces a score, when it is persisted, then both individual scores, the composite score, and abbreviated critiques are written to the cycle's artifact directory
- Given persistence fails, when the error is caught, then a warning is logged but the cycle is not interrupted

### US-04: As the evaluator, I need scores accumulated in a bounded in-memory history, so that trend analysis at checkpoint time has recent data without unbounded memory growth

Evaluation scores from each cycle are appended to an in-memory history list. This history is capped at a maximum number of entries; when the cap is reached, the oldest entry is removed. The bounded history feeds into periodic strategic reviews.

#### Acceptance Criteria
- Given a new score is recorded, when the history already contains the maximum number of entries, then the oldest entry is removed before the new one is added
- Given a new score is recorded, when the history is below the maximum, then the entry is simply appended
- Given the score history contains entries, when a checkpoint runs, then the entries are available as input to the strategic review

---

## Meta-Review

### US-05: As the evaluator, I need a periodic strategic review that analyses score trends, recent version control history, and current notes, so that the agent receives informed course corrections

At each checkpoint, the framework feeds the score history table, recent commit history, and current notes to a strategic review process. The output is a strategic direction statement that gets injected into the agent's system prompt, steering its priorities for the next batch of cycles.

#### Acceptance Criteria
- Given score history, commit history, and notes are available, when the strategic review runs, then a strategic direction text is produced
- Given the strategic review fails, when the error is caught, then the agent proceeds without updated direction (empty direction)
- Given the strategic review produces output, when the next cycle starts, then the direction text appears in the system prompt before the mission

### US-06: As the evaluator, I need the strategic review to receive a formatted score history table, so that quality trends are visible at a glance

The score history is formatted as a tabular summary showing each cycle's individual evaluator scores and composite score. This structured presentation helps the review process identify trends, regressions, and improvements.

#### Acceptance Criteria
- Given scores from multiple cycles exist, when the history is formatted, then a table with cycle number and all score dimensions is produced
- Given no scores have been recorded, when the history is formatted, then a placeholder message indicates no data is available
- Given more than twenty entries exist, when the history is formatted, then only the most recent twenty are included

---

## Feedback Loop

### US-07: As the evaluator, I need evaluation scores and critiques appended to the agent's persistent notes, so that the agent can see and learn from its quality feedback

After each evaluation, a compact summary of the scores and critiques is appended to the notes. When the agent reads its notes at the start of the next cycle, it sees what the evaluators praised and criticized, enabling self-correction over time.

#### Acceptance Criteria
- Given evaluation produced a score with critiques, when notes are appended, then the composite score and abbreviated critiques from both evaluators appear in the cycle's notes entry
- Given the agent reads its notes at the start of the next cycle, when it processes them, then it can see the evaluation feedback from the previous cycle

### US-08: As the evaluator, I need a one-line evaluation summary included in commit messages, so that version control history carries quality context

A compact evaluation summary (scores only, no critiques) is embedded in the commit message alongside metrics and hook status. This makes quality trends visible directly in the version control log without needing to open artifact files.

#### Acceptance Criteria
- Given evaluation produced a score, when the commit message is built, then a one-line score summary is included in the commit body
- Given evaluation did not produce a score, when the commit message is built, then no evaluation line appears

---

## Notes Compression

### US-09: As the evaluator, I need old cycle notes compressed into a concise summary at checkpoint time, so that the agent's memory stays manageable without losing historical context

As the notes file grows across many cycles, older entries become less actionable but still provide useful context. At checkpoint time, entries older than the retention window are compressed into a brief summary, while recent entries are preserved verbatim. This keeps the notes file within a size that fits the agent's context window.

#### Acceptance Criteria
- Given the notes file has accumulated entries beyond the retention threshold, when compression runs at a checkpoint, then older entries are replaced with a condensed summary
- Given compression produces output, when the notes file is rewritten, then the compressed summary precedes the retained recent entries
- Given compression fails, when the error is caught, then the original notes file is left unchanged

---

## Structured MetaReview Decisions

### US-10: As the evaluator, I need the strategic review to produce a structured decision (continue/pivot/stop) alongside its free-text analysis, so that the agent loop can enforce course corrections programmatically

The MetaReview LLM outputs a JSON decision block in addition to the existing six free-text sections. The decision carries an action (`continue`, `pivot`, or `stop`), a reason, and an optional pivot direction. A fault-tolerant parser extracts this block; if parsing fails, the default is `continue` (preserving current behavior).

#### Decision criteria
- **stop**: 3+ consecutive cycles with no code changes AND no score improvement, OR coverage is saturated AND scores are consistently high (>= 8)
- **pivot**: coverage gaps show important untouched areas, OR scores are declining on a specific dimension, OR the agent is repeating the same work
- **continue**: all other cases

#### Acceptance Criteria
- Given the MetaReview LLM produces a valid JSON decision block, when the checkpoint parses the output, then a MetaReviewDecision with action/reason/pivot_direction is returned
- Given the MetaReview LLM does not produce a valid JSON block, when parsing fails, then the default decision is `continue` and a warning is logged
- Given the decision is `stop`, when the agent loop processes the checkpoint result, then the loop terminates with mission_status="stopped"
- Given the decision is `pivot`, when the agent loop processes the checkpoint result, then a PIVOT DIRECTIVE is prepended to the strategic direction context
- Given the decision is `continue`, when the agent loop processes the checkpoint result, then behavior is unchanged from before this feature

### US-11: As the evaluator, I need cross-cycle file coverage tracking that reports which project files have been read or written across all cycles, so that MetaReview can make data-driven pivot decisions

A CoverageTracker accumulates the set of files read and written across all cycles. At checkpoint time, it computes a coverage report comparing touched files against the full project file inventory. The report includes coverage ratio and a list of untouched important files (capped at 50). This report is injected into the MetaReview prompt.

#### Acceptance Criteria
- Given the agent has run N cycles, when a checkpoint occurs, then the coverage report reflects all files read or written across all N cycles (not just the latest)
- Given a coverage report is computed, when it is formatted, then it includes total project files, files touched, coverage ratio, and up to 50 untouched files
- Given the coverage report is formatted, when the MetaReview LLM runs, then the report appears in the user prompt between git delta and agent notes

### US-12: As the framework, I need the agent loop to enforce MetaReview decisions as control-flow actions, so that diminishing returns are addressed without relying on the agent's own judgment

After each checkpoint, the agent loop inspects the MetaReviewDecision. A `stop` decision terminates the loop gracefully (like MISSION COMPLETE but externally triggered). A `pivot` decision updates the strategic direction with a prominent directive. A `continue` decision leaves behavior unchanged.

#### Acceptance Criteria
- Given MetaReview returns action="stop", when the loop processes it, then the loop breaks with mission_status="stopped" and the reason is logged
- Given MetaReview returns action="pivot" with a pivot_direction, when the loop processes it, then the next cycle's system prompt contains a PIVOT DIRECTIVE block before the existing strategic direction
- Given MetaReview returns no decision (None), when the loop processes it, then behavior is identical to the pre-feature baseline
