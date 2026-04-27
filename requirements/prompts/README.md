# Prompts Domain

This domain covers the evaluation and meta-review prompt templates used by the harness. There are two evaluation modes (code-change and reasoning-only), each with two perspectives (quality and impact/safety), plus a periodic meta-review and a notes compression mechanism.

---

## US-01: As the evaluator, I need to assess code-change proposals on four weighted dimensions so that scoring reflects the relative importance of correctness over style

A code-change evaluator must score every proposal across four dimensions with fixed weights: correctness at 40%, completeness at 30%, specificity at 20%, and architecture fit at 10%. The final score is the weighted average of these four dimension scores minus any applicable penalties. This weighting ensures that a proposal which is correct but vaguely written scores higher than one which is specific but wrong.

### Acceptance Criteria
- Given a code-change proposal, when the evaluator produces a score, then the output contains individual dimension scores (each 0-10) and an explicit weighted arithmetic calculation
- Given dimension scores A, B, C, D, when the weighted average is computed, then the formula is (A x 0.40) + (B x 0.30) + (C x 0.20) + (D x 0.10) minus penalties
- Given any evaluation, when the final score is reported, then it is rounded to one decimal place

---

## US-02: As the evaluator, I need a decision tree that anchors scores to concrete evidence gates so that borderline proposals are scored consistently

Without structured anchoring, evaluators tend to cluster scores around the middle of the scale. The evaluator must apply a sequential gate system: proposals that fail to name specific code entities cannot exceed 4.0; those without major functionality cannot exceed 5.5; those without edge-case handling and testability evidence cannot exceed 6.5; and those not ready for code review cannot exceed 7.5. Half-point scores are reserved for borderline cases where evidence exists for both sides of a gate.

### Acceptance Criteria
- Given a proposal that does not reference any specific file or function, when scored, then the score must be 4.0 or below
- Given a proposal that names code entities but lacks working major functionality, when scored, then the score must be between 4.5 and 5.5
- Given a proposal that works but lacks edge-case coverage or testability evidence, when scored, then the score must be between 6.0 and 6.5
- Given a proposal that is fully achieved and review-ready, when scored, then the score must be 8.0 or above
- Given a half-point score, when the evaluation is produced, then evidence for both the passing and failing side of the relevant gate must be cited

---

## US-03: As the evaluator, I need anti-inflation and anti-deflation rules so that extreme scores are only assigned with explicit justification

Scores at the extremes (very high or very low) carry outsized influence on strategic decisions. A score of 9 or 10 must include an explicit statement of what specifically makes the proposal near-perfect; if the evaluator cannot name a concrete reason, the maximum score is 8. Conversely, a score below 4 must cite a specific defect -- a wrong line, missing function, or broken assumption -- and must not penalize heavily for style or missing tests alone.

### Acceptance Criteria
- Given a score of 9 or 10, when the evaluation is produced, then it must contain an explicit justification naming what makes the proposal exceptional
- Given a score below 4, when the evaluation is produced, then it must cite at least one concrete defect with a specific location or construct
- Given a proposal with only stylistic issues and no functional defects, when scored, then the score must be 4 or above

---

## US-04: As the evaluator, I need to apply fixed penalties for specific structural failures so that missing references and absent sections always reduce the score

Certain failures are so fundamental that they must always cost points regardless of other qualities. The evaluator must deduct 3 points when no concrete code entity from the source context is cited, 2 points when a required section of the task is entirely absent, and 1 point for each static analysis error that was already surfaced in the evaluation context. The score floor after penalties is zero.

### Acceptance Criteria
- Given a proposal that cites no file, function, or class from the source context, when scored, then a 3-point penalty is applied
- Given a proposal that omits an entire required section, when scored, then a 2-point penalty is applied
- Given a context containing N static analysis errors, when scored, then an N-point penalty is applied (1 per error)
- Given penalties that would push the score below zero, when the final score is computed, then it is clamped to zero

---

## US-05: As the evaluator, I need to compare proposals against a prior best when one exists so that improvement or regression across rounds is tracked

In multi-round evaluation, knowing whether a new proposal improves on the previous best is as important as the absolute score. When a prior best is present in the context, the evaluator must compare dimension-by-dimension and label each as improved, regressed, or unchanged with a reason. Additionally, if a proposal repeats a defect that was already flagged as the top defect of the prior best, an extra 1-point penalty on the correctness dimension must be applied -- repeating a known issue is worse than introducing a new one.

### Acceptance Criteria
- Given a prior best in the context, when the evaluation is produced, then a delta section appears showing the direction of change for each dimension
- Given no prior best in the context, when the evaluation is produced, then the delta section is omitted entirely
- Given a proposal that repeats the prior best's identified top defect, when scored, then correctness loses an additional 1 point beyond normal scoring

---

## US-06: As the evaluator, I need a structured output format with specific labeled sections so that downstream consumers can reliably parse results

Evaluation output must follow a rigid structure so that automated systems can extract scores, defects, and feedback. The required sections for code-change evaluation are: an optional delta-vs-prior section, an analysis section with per-dimension breakdowns, a top defect section identifying the single most critical issue, actionable feedback with prioritized fixes, a statement of what would make the proposal perfect, and the final numeric score.

### Acceptance Criteria
- Given any code-change evaluation, when the output is produced, then it contains at minimum: analysis with four scored dimensions, top defect, actionable feedback, what-would-make-this-perfect, and score
- Given the score line, when parsed, then it contains exactly one numeric value rounded to one decimal place
- Given the actionable feedback section, when produced, then items are numbered and reference specific locations and changes needed

---

## US-07: As the evaluator, I need to assess reasoning-only cycles (no code changes) on exploration-oriented dimensions so that cycles without commits are still meaningfully scored

When an agent cycle produces no code changes, the standard code-quality dimensions are irrelevant. Instead, the evaluator must score across four reasoning-specific dimensions: exploration thoroughness at 35% (did the agent actively investigate the codebase), judgment quality at 30% (is the conclusion evidence-based), direction discovery at 20% (did the agent identify valuable next steps), and information density at 15% (does the output contain new information versus repetition).

### Acceptance Criteria
- Given an agent cycle with no code changes, when evaluated in reasoning mode, then the four dimensions are exploration thoroughness, judgment quality, direction discovery, and information density
- Given dimension scores, when the weighted average is computed, then the formula is (A x 0.35) + (B x 0.30) + (C x 0.20) + (D x 0.15) minus penalties
- Given an agent that read zero files and declared completion, when scored on exploration thoroughness, then it receives 0 on that dimension
- Given an agent whose conclusion lacks supporting evidence, when scored on judgment quality, then it receives 3 or below on that dimension

---

## US-08: As the evaluator, I need reasoning-mode penalties for stale repetition and passive behavior so that agents that loop without progress are penalized

An agent that produces the same "nothing to do" output cycle after cycle, or that generates text without actually using any tools to explore, is consuming resources without value. The evaluator must deduct 3 points when the agent's output is essentially identical to the previous cycle's, and 2 points when no tool calls were made (pure text generation without exploration).

### Acceptance Criteria
- Given an agent whose output closely matches the previous cycle, when scored, then a 3-point penalty is applied for stale repetition
- Given an agent that made no tool calls during the cycle, when scored, then a 2-point penalty is applied for lack of exploration
- Given both conditions simultaneously, when scored, then both penalties apply (total 5 points deducted)

---

## US-09: As the evaluator, I need to assess the second-order safety impact of code-change proposals so that downstream and systemic risks are surfaced

Beyond whether a change is correct, a separate evaluation must assess consequences beyond the directly touched code. This impact evaluator assumes the proposal is correctly implemented and focuses on four dimensions: caller impact at 35% (how callers and dependents are affected), maintenance debt at 30% (ongoing cost imposed), emergent behavior at 20% (non-obvious effects at scale or under stress), and rollback safety at 15% (difficulty of reverting in production). Higher scores indicate safer, more contained changes.

### Acceptance Criteria
- Given any code-change proposal, when assessed for impact, then four dimensions are scored: caller impact, maintenance debt, emergent behavior, and rollback safety
- Given dimension scores, when the weighted average is computed, then the formula is (A x 0.35) + (B x 0.30) + (C x 0.20) + (D x 0.15) minus penalties
- Given a change that is purely internal to one module with no external dependencies, when assessed, then it receives a high score (7 or above) indicating safety
- Given a change that modifies a public API used by many callers without providing compatibility shims, when assessed, then it receives a low score (4 or below) indicating risk

---

## US-10: As the evaluator, I need an impact-specific decision tree so that containment and rollback safety are assessed consistently

The impact evaluator must apply its own sequential gate system, distinct from the quality evaluator's gates. Changes that modify a public API or contract used by three or more callers without a compatibility shim cannot exceed 4. Changes requiring coordinated modifications across multiple files cannot exceed 6. Changes where rollback requires data migration or format versioning cannot exceed 5. Changes whose effects are provably bounded to a single module score 7 or above.

### Acceptance Criteria
- Given a change to a public API used by three or more callers with no shim, when assessed, then the score is 4 or below
- Given a change requiring coordinated edits in two or more files to be safe, when assessed, then the score is 6 or below
- Given a change where rollback requires data migration, when assessed, then the score is 5 or below
- Given a change whose effects are provably bounded to one module, when assessed, then the score is 7 or above

---

## US-11: As the evaluator, I need impact-mode penalties for pre-existing static analysis errors so that broken code is not given misleadingly optimistic safety scores

When static analysis has already found errors in the changed code, second-order analysis built on that broken foundation would produce artificially optimistic impact scores. The impact evaluator must deduct 1 point per static analysis error already surfaced in the evaluation context, ensuring that objectively broken code is not rated as safe to deploy.

### Acceptance Criteria
- Given N static analysis errors in the evaluation context, when the impact score is computed, then N points are deducted (1 per error)
- Given no static analysis errors, when the impact score is computed, then no error-based penalty is applied
- Given penalties that would push the score below zero, when the final score is computed, then it is clamped to zero

---

## US-12: As the evaluator, I need to track impact-dimension deltas against a prior best so that regression on known risks is penalized more severely

When a prior best exists, the impact evaluator must compare each dimension and note the direction of change. If a proposal reintroduces a risk that was already identified as the key risk of the prior best, the caller impact dimension loses an additional point. Regression on a previously identified and mitigated risk is a more serious failure than encountering a fresh risk.

### Acceptance Criteria
- Given a prior best in the context, when the impact evaluation is produced, then a delta section appears with direction labels for each of the four impact dimensions
- Given a proposal that reintroduces the prior best's key risk, when scored, then caller impact loses an additional 1 point
- Given no prior best, when the impact evaluation is produced, then the delta section is omitted

---

## US-13: As the evaluator, I need a structured impact output format with risk-focused sections so that mitigations can be actioned

The impact evaluation output must follow a rigid structure: an optional delta-vs-prior section, an analysis section with per-dimension scores and arithmetic, a key risk section identifying the single most significant second-order concern with a concrete mitigation, numbered actionable mitigations, a statement of what architectural or systemic change would eliminate the primary risk, and the final numeric score.

### Acceptance Criteria
- Given any impact evaluation, when the output is produced, then it contains: analysis with four scored dimensions, key risk, actionable mitigations, what-would-make-this-perfect, and score
- Given the key risk section, when a score is below 9, then it identifies a specific scenario and a concrete mitigation step
- Given a score of 9 or above, when the key risk section is produced, then it may state "none"

---

## US-14: As the evaluator, I need to assess the safety of reasoning-only cycles from a systems perspective so that stagnation and missed opportunities are detected

When an agent produces no code changes, a separate systems-level evaluation must determine whether inaction is justified and what second-order effects this has on project trajectory. This evaluator scores four dimensions: stagnation risk at 35% (is the agent entering a repetitive loop), opportunity cost at 30% (what improvements exist but are not being pursued), strategic value at 20% (did the exploration add value despite no code output), and trajectory health at 15% (is the project heading toward productive cycles or repeating empty patterns). Higher scores indicate that inaction is justified and the project is healthy.

### Acceptance Criteria
- Given an agent cycle with no code changes, when assessed from a systems perspective, then four dimensions are scored: stagnation risk, opportunity cost, strategic value, and trajectory health
- Given a single empty cycle after major work, when assessed for stagnation risk, then it receives a high score (8 or above) indicating low risk
- Given three or more consecutive empty cycles with similar notes, when assessed for stagnation risk, then it receives a low score (4 or below) indicating high risk
- Given obvious improvements that exist but the agent claims nothing to do, when assessed for opportunity cost, then the conclusion is flagged as incorrect

---

## US-15: As the evaluator, I need reasoning-mode impact penalties for sustained inaction and vague planning so that chronically idle agents are scored down

Sustained inaction and lack of concrete planning signal that the agent has lost direction. The impact evaluator for reasoning-only cycles must deduct 2 points when this is the third or more consecutive cycle with no code changes, and 1 point when the agent's notes fail to mention any concrete next steps.

### Acceptance Criteria
- Given the third consecutive cycle with no code changes, when scored, then a 2-point penalty is applied
- Given that the agent's notes contain no concrete next steps, when scored, then a 1-point penalty is applied
- Given a first or second empty cycle with concrete next steps documented, when scored, then neither penalty applies

---

## US-16: As the meta-reviewer, I need to analyze score trends and agent history at periodic checkpoints so that strategic direction can be adjusted before problems compound

The meta-reviewer operates on a periodic schedule (every N cycles) and receives evaluation scores from recent cycles, a git history delta, and the agent's accumulated notes. Its purpose is to identify patterns, diagnose recurring issues, and produce a concise strategic direction adjustment. When scores are consistently high, it should suggest stretch goals. When scores are dropping, it should diagnose root causes and propose corrective actions. When the agent has repeated cycles with no code changes, it must recognize that the current direction is exhausted and propose entirely new focus areas.

### Acceptance Criteria
- Given consistently high scores (8 or above), when the meta-review is produced, then it acknowledges success and suggests stretch goals or new focus areas
- Given declining scores, when the meta-review is produced, then it diagnoses a specific root cause and proposes a concrete corrective action
- Given repeated cycles with no code changes, when the meta-review is produced, then the direction adjustment proposes entirely new focus areas drawn from unexplored parts of the codebase
- Given no evaluation scores available, when the meta-review is produced, then analysis focuses on git delta and agent notes instead of score trends

---

## US-17: As the meta-reviewer, I need to produce output in six mandatory sections so that the agent receives structured, actionable guidance

The meta-review output must contain exactly six sections: a progress summary listing concrete deliverables (files changed, features added, bugs fixed), a score trend analysis calling out consistently weak dimensions, recurring issues naming specific patterns or areas, what worked (approaches that produced the highest scores), gaps (important work being neglected), and a direction adjustment with concrete instructions for the next batch of cycles. The entire output must stay under 500 words because the agent reads it every cycle until the next review.

### Acceptance Criteria
- Given any meta-review, when the output is produced, then it contains exactly six sections: progress summary, score trend, recurring issues, what worked, gaps, and direction adjustment
- Given the direction adjustment section, when produced, then it contains specific actionable instructions (not generic advice) for the next batch of cycles
- Given any meta-review, when the output is produced, then the total word count is under 500

---

## US-18: As the meta-reviewer, I need to compress accumulated agent notes into a concise topical summary so that context stays lean without losing critical knowledge

Over many cycles, per-cycle notes accumulate and become too large for the agent to read efficiently each cycle. The notes compressor must distill old notes into a summary that preserves key decisions and rationale, important codebase findings, accomplishments, recurring problems, and the trajectory of the work. It must discard redundant repetitions, routine status updates, raw individual score numbers, and tool call timing details. The output must be organized by topic (not by cycle number) and stay under 800 words.

### Acceptance Criteria
- Given accumulated notes from many cycles, when compressed, then the summary preserves key decisions, codebase findings, accomplishments, recurring problems, and work trajectory
- Given the compressed output, when organized, then it uses topical headers (not cycle-number headers)
- Given any compression, when the output is produced, then the total word count is under 800
- Given redundant findings repeated across multiple cycles, when compressed, then they appear only once in the summary
- Given raw individual score numbers in the notes, when compressed, then they are discarded (only trends are preserved)

---

## US-19: As the evaluator, I need the reasoning-only output format to be simpler than the code-change format so that the evaluation focuses on what matters for exploration cycles

Reasoning-only evaluations deal with agent behavior rather than code artifacts, so the output structure is streamlined. The required sections are: analysis with four dimension scores and arithmetic, a top issue identifying the single most important thing the agent should have done differently, actionable feedback describing what the agent should do next and what exploration was missed, and the final numeric score. There is no delta-vs-prior section and no what-would-make-this-perfect section.

### Acceptance Criteria
- Given a reasoning-only evaluation, when the output is produced, then it contains: analysis, top issue, actionable feedback, and score
- Given a reasoning-only evaluation, when the output is produced, then it does not contain a delta-vs-prior section or a what-would-make-this-perfect section
- Given the actionable feedback section in a reasoning-only evaluation, when produced, then it includes what the agent should do next cycle and what exploration was missed
