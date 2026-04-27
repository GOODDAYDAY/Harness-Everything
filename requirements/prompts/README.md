# Prompts Domain

The prompts domain governs the text that directs LLM evaluators and the meta-review advisor. These prompts are the highest-leverage surface in the entire harness: a one-word change in a rubric propagates through every evaluation of every cycle of every future run. The requirements here describe what the prompts must accomplish, not how they are worded.

Two source files comprise this domain:

- `harness/prompts/dual_evaluator.py` -- four prompt templates that drive the dual-isolated evaluation system.
- `harness/prompts/agent_meta_review.py` -- one system prompt and one user template that drive periodic strategic reviews.

---

## Evaluator Prompts

The dual evaluation system runs two independent LLM calls per cycle. Two of the four prompts evaluate cycles that produced code changes; two evaluate cycles where the agent explored but changed nothing. The system selects the appropriate pair based on whether a git diff exists.

### Why two perspectives exist

A single evaluator cannot reliably judge both "is this code correct?" and "will this change break something else?" Correctness evaluation anchors on the diff itself; diffusion evaluation assumes the diff is correct and asks about ripple effects. Keeping these perspectives isolated prevents a halo effect where one strong dimension inflates the other.

### Why reasoning-mode prompts exist separately

A cycle with no code changes is not inherently bad -- the agent may have explored the codebase and left valuable notes. But a cycle with no changes and no exploration is wasted compute. The reasoning-mode prompts must judge the quality of thinking rather than the quality of code, which requires entirely different evaluation dimensions and a different scoring scale.

---

### Scenario: Code-change evaluation (Basic perspective)

**Context:** The agent has completed a cycle that modified files. The framework invokes the basic evaluator with the git diff, source context, and any static analysis findings.

**What the prompt must accomplish:**

1. **Structured multi-dimensional scoring.** The evaluator must assess four distinct dimensions -- correctness, completeness, specificity, and architecture fit -- each scored independently on a 0-10 scale. A single overall "this is good" judgment is insufficient; the agent needs to know which dimension to improve.

2. **Weighted composition with explicit arithmetic.** The final score must be a weighted average of the four dimensions (correctness weighted heaviest, architecture fit lightest), computed with visible arithmetic. This prevents the evaluator from "gut-feeling" a score that contradicts its own dimensional analysis.

3. **Anti-inflation.** Scores of 9 or 10 must require the evaluator to state a concrete reason why the output is near-perfect. Without this constraint, LLM evaluators trend toward high scores because complimenting is easier than criticising.

4. **Anti-deflation.** Scores below 4 must require evidence of a concrete defect -- a specific wrong line, missing function, or broken assumption. Without this constraint, an overly cautious evaluator can tank scores for stylistic preferences.

5. **Anchoring through a decision tree.** The prompt must define a gate-based decision tree that forces the evaluator to answer binary questions in sequence (e.g., "Does it name specific files/functions? No -> score at most 4.0"). This prevents evaluators from jumping to a score without systematically working through the criteria.

6. **Penalty mechanics.** The prompt must define explicit point deductions for objectively bad signals (no concrete code references cited, required sections missing, known static analysis errors). Penalties are applied after the weighted average and cannot reduce the score below 0.

7. **Prior-round comparison.** When a "Prior Best" from a previous evaluation round is present, the evaluator must compare against it dimension-by-dimension and note improvement or regression. Repeating a defect already flagged in the prior best incurs an additional penalty. This drives iterative improvement rather than oscillation.

8. **Actionable output structure.** The evaluator must produce its output in a fixed section format: analysis per dimension, top defect, actionable feedback, what-would-make-10/10, and the final score. This structure exists so downstream consumers (the agent's notes, the meta-review) can reliably parse the feedback.

**Acceptance criteria:**

- The evaluator never produces a score without showing the weighted arithmetic that derived it
- Scores of 9+ always include an explicit justification sentence
- Scores below 4 always cite a specific defect (file, function, or line)
- The decision tree gates are applied in order; a "no" at gate 1 caps the score regardless of later gates
- Static analysis ERROR findings from the prompt context trigger a penalty; the evaluator cannot give a high score to code that fails to compile
- When prior best is present, the delta section appears before the analysis; when absent, the delta section is omitted entirely
- The output includes exactly the sections specified, in order, with no extras

---

### Scenario: Code-change evaluation (Diffusion perspective)

**Context:** Same cycle as above, but this evaluator assesses second-order effects rather than direct correctness.

**What the prompt must accomplish:**

1. **Assume correctness, assess consequences.** The diffusion evaluator must take the proposal as implemented correctly and judge what happens beyond the directly touched code: caller breakage, maintenance burden, emergent behaviour under load, rollback difficulty.

2. **Containment-focused scoring scale.** A high diffusion score (9-10) means "negligible systemic impact, trivial rollback." A low score (0-2) means "catastrophic cascade, irreversible damage." The scale runs in the same direction as the basic evaluator (higher is better), but measures containment safety rather than code quality. A 10 means negligible systemic impact; a 0 means catastrophic cascade.

3. **Gate-based anchoring for systemic risk.** The prompt must define gates around public API changes, cross-file coordination requirements, rollback complexity, and module containment. These gates prevent the evaluator from hand-waving away systemic risk.

4. **Concrete caller identification.** The evaluator must name specific callers visible in the source context, not abstract about "code that depends on this." Vague diffusion analysis provides no actionable signal.

5. **Anti-inflation and anti-deflation symmetric with basic.** The same discipline applies: near-perfect scores need justification, and low scores need evidence. Manufacturing negative findings to appear thorough is explicitly prohibited.

6. **Prior-round regression tracking.** Reintroducing a risk already identified as the KEY RISK in the prior best incurs an extra penalty. Regression on a known risk is worse than a fresh risk.

7. **Mitigation-oriented output.** The output must include concrete mitigations, not just problems. Each mitigation should name a file, function, and the specific guard needed.

**Acceptance criteria:**

- The evaluator never assesses whether the code is correct -- it assumes correctness and evaluates impact
- Every caller impact analysis names at least one specific caller from the source context, or explicitly states none are visible
- The KEY RISK section uses the format "FILE::function -- scenario -> mitigation" when a risk exists
- Static analysis ERROR findings trigger a penalty even in diffusion evaluation (broken code makes impact analysis meaningless)
- Rollback safety assessment considers persisted format changes, not just code revert difficulty

---

### Scenario: Reasoning-only evaluation (Basic perspective)

**Context:** The agent completed a cycle with no code changes. The framework invokes the reasoning-mode basic evaluator.

**What the prompt must accomplish:**

1. **Judge exploration quality, not code quality.** The scoring dimensions shift entirely: exploration thoroughness, judgment quality, direction discovery, and information density replace correctness/completeness/specificity/architecture.

2. **Detect zero-effort cycles.** An agent that reads zero files and declares "mission complete" must score at most 1-2. The prompt must make this explicit so the evaluator does not reward confident-sounding but empty output.

3. **Value useful notes.** An agent that explored thoroughly and left concrete findings for the next cycle (specific files, patterns, next steps) should score well even though no code changed. The scoring guide must recognize that good exploration is productive work.

4. **Penalise stale repetition.** If the agent's output is essentially identical to the previous cycle's, a penalty applies. This catches agents stuck in a loop of "nothing to do" declarations.

5. **Penalise pure text generation.** If no tool calls were made, the agent generated text without actually looking at the codebase, which deserves a penalty.

**Acceptance criteria:**

- The evaluator does not penalise the absence of code changes -- that is the expected condition for this prompt mode
- An agent with zero tool calls always receives at least a -2 penalty
- The scoring guide explicitly distinguishes between "explored and correctly concluded nothing to change" (score 6+) and "declared nothing to change without exploring" (score 3 or below)

---

### Scenario: Reasoning-only evaluation (Diffusion perspective)

**Context:** Same no-code-change cycle, but assessed for systemic trajectory risk.

**What the prompt must accomplish:**

1. **Detect stagnation patterns.** The evaluator must look across recent cycle history: a single empty cycle after major work is healthy (score 7+), but three consecutive empty cycles with similar notes is alarming (score 4 or below).

2. **Assess opportunity cost.** The evaluator must consider what improvements exist that the agent is not pursuing -- missing tests, error handling, documentation, performance. If obvious improvements exist, the "nothing to do" conclusion is wrong.

3. **Evaluate trajectory health.** Based on agent notes and recent history, will the next cycle be productive, or will it repeat the same empty pattern? The evaluator must make a forward-looking judgment.

4. **Penalise consecutive inaction.** Three or more consecutive cycles with no code changes trigger an automatic penalty. Notes without concrete next steps trigger another.

**Acceptance criteria:**

- The evaluator considers cycle history, not just the current cycle in isolation
- Stagnation risk scoring distinguishes between "one pause after productive work" and "stuck in a loop"
- Opportunity cost analysis names specific areas of the codebase being neglected, not generic suggestions

---

### Cross-cutting requirements for all evaluator prompts

These requirements apply to all four evaluator prompt templates.

1. **Mode selection must be automatic and correct.** The framework selects code-change or reasoning-mode prompts based on whether a git diff exists. The prompts themselves do not need to handle mode detection, but they must not produce nonsensical output if invoked in the wrong mode (e.g., a code evaluator receiving an empty diff should not crash the evaluation).

2. **Score must be parseable.** Every prompt must produce a `SCORE: X.X` line that can be extracted by a simple regex. The score is always rounded to one decimal place. This is a hard contract with the evaluation engine's score parser.

3. **Dimensional weights must sum to 1.0.** Each prompt's dimension weights (e.g., 0.40 + 0.30 + 0.20 + 0.10 = 1.00) must be mathematically consistent. The evaluator shows its arithmetic, so incorrect weights would produce visible contradictions.

4. **Prompts must not leak into each other.** The basic and diffusion evaluators run in isolated LLM calls. Neither prompt should reference or anticipate the other's output. This isolation is what makes dual evaluation meaningful -- if one evaluator knew what the other said, the two scores would not be independent.

---

## Meta-Review Prompt

The meta-review runs periodically (every N committed cycles, configurable) and serves a fundamentally different purpose from per-cycle evaluation: it identifies patterns across multiple cycles and produces strategic direction adjustments.

### Why meta-review exists

Per-cycle evaluation tells the agent "this specific change was good/bad." But it cannot tell the agent "you've been editing the same file for five cycles and ignoring tests" or "your scores are dropping because you stopped reading code before editing." Pattern recognition across cycles requires a broader view, which is the meta-review's job.

### Scenario: Strategic direction adjustment

**Context:** The framework has accumulated score history, git deltas, and agent notes from the last several cycles. It invokes the meta-review to produce guidance that will be injected into subsequent cycles' system prompts.

**What the prompt must accomplish:**

1. **Concrete pattern identification.** The meta-review must name specific files, functions, and metrics -- not "the agent should try harder." If scores are declining, it must diagnose the root cause (e.g., "Completeness has been below 6 for 3 cycles because the agent is not running tests after edits").

2. **Acknowledge success.** If scores are consistently high (8+), the meta-review must recognise this and suggest stretch goals or new focus areas rather than manufacturing problems.

3. **Detect exhausted directions.** If the agent's notes show repeated cycles with no code changes, the current direction is exhausted. The meta-review must propose entirely new focus areas based on unexplored parts of the codebase -- not repeat the same guidance.

4. **Graceful degradation without scores.** If no evaluation scores are available (e.g., evaluation is disabled in config), the meta-review must still function, basing its analysis on git deltas and agent notes instead.

5. **Structured six-section output.** The meta-review must produce exactly six sections: Progress Summary, Score Trend, Recurring Issues, What Worked, Gaps, and Direction Adjustment. This structure ensures all relevant angles are covered and the output is predictable for the agent consuming it.

6. **Brevity constraint.** The output must stay under 500 words because the agent reads this every cycle until the next review. Verbose guidance wastes context window on every subsequent cycle.

7. **Actionable direction adjustment.** The Direction Adjustment section must contain concrete instructions for the next 3-5 cycles: "Focus on X before moving to Y," "Stop doing Z," "The weakest dimension is A, prioritise it by doing B." Generic advice like "write better code" is useless.

**Acceptance criteria:**

- The meta-review names specific files, functions, or metrics in at least three of its six sections
- When scores are consistently high, the output acknowledges success rather than inventing problems
- When consecutive empty cycles are detected, the Direction Adjustment section proposes new focus areas, not repetitions of the current direction
- When no score history is available, the Score Trend section explicitly states this and the analysis shifts to git delta and notes
- The output contains exactly six sections with the specified headings
- The Direction Adjustment section contains at least two concrete instructions with specific targets (files, dimensions, or behaviours)
