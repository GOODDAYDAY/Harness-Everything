# Agent Evaluation -- Requirements

The evaluation system provides automated quality feedback on every cycle's output. Its purpose is to create a closed loop: the agent produces work, the framework scores it, the scores appear in the agent's notes, and the agent adjusts its behavior accordingly.

---

## R-EVAL-01: Dual isolated evaluation

Every committed cycle must be evaluated by two independent evaluators that run in parallel and never see each other's output. One evaluator focuses on correctness and completeness ("basic"), the other on system-level impact and risk ("diffusion"). Their scores are combined into a single composite score.

**Why:** A single evaluator develops blind spots -- it may consistently overvalue correctness while ignoring maintenance debt, or vice versa. Two independent perspectives with different rubrics catch more issues. Isolation (neither sees the other's output) prevents groupthink: if both evaluators could see each other, they would converge to similar opinions and lose the diversity benefit.

**Acceptance criteria:**
- Both evaluators run concurrently (not sequentially) to minimize latency.
- If one evaluator's LLM call fails, the other is cancelled rather than left running as an orphan task.
- The composite score is a weighted combination (not a simple average), with fixed weights favoring the basic evaluator.
- Scores are on a 0-10 scale. Score parsing clamps to that range if the LLM outputs out-of-range values; the composite score computation additionally rejects out-of-range inputs as a defense-in-depth measure.

---

## R-EVAL-02: Evaluation mode adaptation

The evaluation must adapt its rubric based on what the cycle produced. When the cycle committed code changes, the evaluators review the git diff ("implement" mode). When the cycle produced no code changes (exploration, planning), the evaluators review the agent's reasoning output ("reasoning" mode).

**Why:** Evaluating a planning cycle with a code-quality rubric produces meaninglessly low scores that discourage the agent from ever exploring or planning. Conversely, evaluating a code change with a reasoning rubric misses syntax errors and test failures. The mode must match the deliverable.

**Acceptance criteria:**
- A cycle that commits code changes is evaluated in implement mode. The evaluators receive the staged git diff as input.
- A cycle that produces no code changes is evaluated in reasoning mode. The evaluators receive the agent's text output as input.
- An empty cycle (no text output and no code changes) is evaluated with a placeholder indicating the cycle was empty, rather than skipping evaluation entirely.

---

## R-EVAL-03: Score persistence and history

Each cycle's evaluation scores must be persisted to the artifact store as structured JSON and appended to the agent's persistent notes in a compact format. The framework must also maintain an in-memory score history (capped to prevent unbounded growth) for trend analysis.

**Why:** Scores serve three consumers: (1) the artifact store for post-hoc analysis, (2) the persistent notes for the agent's self-awareness, (3) the in-memory history for meta-review. Each consumer needs a different format -- JSON for machines, one-liner for notes, table for meta-review. Persistence failures must not disrupt the cycle.

**Acceptance criteria:**
- After evaluation, the cycle's artifact directory contains `eval_scores.json` with basic score, diffusion score, combined score, and truncated critiques.
- The agent's notes include a compact `[eval]` line with all three scores and abbreviated critique text.
- The in-memory history retains at most a fixed number of entries (e.g., 50). When the cap is reached, the oldest entry is dropped.
- A failure to persist scores is logged but does not crash the cycle or prevent the commit.

---

## R-EVAL-04: Commit message enrichment

Evaluation scores must be included in the git commit message body so that git history itself becomes a queryable record of quality trends. The format must be machine-parseable (e.g., `eval: basic=7.2 diffusion=6.8 combined=7.0`).

**Why:** Operators and CI scripts frequently use `git log` to understand what happened. Embedding scores in commit messages means quality trends are visible without loading separate artifact files. The machine-parseable format enables scripts like "show me all commits where combined score dropped below 5.0".

**Acceptance criteria:**
- Every commit message produced by the framework includes an `eval:` line in the body when evaluation is enabled.
- The line contains basic, diffusion, and combined scores with one decimal place.
- When evaluation is disabled or fails, the commit message omits the eval line rather than including placeholder values.

---

## R-EVAL-05: Periodic meta-review

At configurable intervals (every N committed cycles), the framework must run a separate LLM call that analyzes score trends, git history, and the agent's notes to produce strategic direction guidance. This guidance is injected into subsequent cycles' system prompts.

**Why:** Cycle-level evaluation tells the agent "this cycle was good/bad" but cannot identify systemic patterns like "you keep creating unused imports" or "you're spending too many cycles on low-value formatting changes". The meta-review looks across multiple cycles to surface these patterns and adjust the agent's priorities.

**Acceptance criteria:**
- The meta-review runs after every N cycles (configurable, e.g., every 5). Setting the interval to 0 disables it.
- The meta-review input includes: the score history table (last 20 entries), git log + diffstat since the last review, and the agent's current notes (truncated to a reasonable size).
- The meta-review output is persisted to the cycle's artifact directory as `meta_review.md`.
- The strategic direction text is injected into subsequent cycles' system prompts under a "Strategic Direction" section, positioned before the mission statement.
- If the meta-review LLM call fails, the strategic direction is reset to empty (the failure result clears the previous direction). The review hash is not updated, so the next interval will retry with the same baseline. Note: this clearing behavior may be unintended -- retaining the previous direction on failure would be more conservative.

---

## R-EVAL-06: Quality feedback loop closure

The evaluation system must form a closed feedback loop: scores and critiques appear in the agent's persistent notes, and the agent's system prompt explicitly instructs it to review evaluation feedback and adjust its approach.

**Why:** Evaluation without feedback is just logging. The whole point is behavior change. The agent must be told that it is being evaluated, what dimensions are scored, and where to find its scores. Without this, evaluation data accumulates in artifacts but never influences the agent's decisions.

**Acceptance criteria:**
- The agent's base system prompt includes a section explaining the 8 evaluation dimensions and telling the agent to review scores in its notes.
- Evaluation notes include not just scores but critique excerpts, so the agent can understand *why* it scored as it did.
- The meta-review strategic direction (when present) appears in the system prompt where the agent will read it before starting its cycle.

---

## R-EVAL-07: Evaluation resilience

Evaluation failures must never block the commit or crash the cycle. The evaluation system is advisory -- it enhances the agent's self-awareness but is not in the critical path.

**Why:** The evaluation runs LLM calls, which can fail (rate limits, timeouts, malformed output). If evaluation failure prevented commits, a temporary API issue would cause the agent to lose all its work for that cycle. The framework treats evaluation as best-effort.

**Acceptance criteria:**
- When the evaluator LLM call raises an exception, the cycle continues without scores. A warning is logged.
- When score parsing fails (no score found in evaluator output), a score of 0.0 is returned rather than crashing.
- When score persistence fails, the commit still proceeds with whatever eval data was successfully collected.
- The commit message omits the eval line when evaluation produced no results, rather than including corrupt data.
