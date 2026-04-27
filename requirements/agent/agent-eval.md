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
