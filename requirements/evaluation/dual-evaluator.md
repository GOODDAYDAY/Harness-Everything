# Dual Evaluator

## Purpose

The dual evaluator is the system's quality gate. Every cycle -- whether it produced a text proposal, executed code, or explored without changes -- passes through this gate before the system decides what to do next. The evaluator's score directly controls iteration: keep the current direction, backtrack, or escalate.

A bad evaluator is worse than no evaluator. An evaluator that always says "8/10, good job" teaches the system nothing. An evaluator that hallucinates defects in correct code wastes cycles fixing phantom bugs. The requirements below exist to make the evaluator reliable enough to trust.

---

## R-01: Independent dual evaluation

Each cycle output must be evaluated by two independent evaluators that never see each other's output. Their scores are combined into a single weighted score.

**Why:** A single evaluator develops blind spots. It might consistently overlook import errors, or consistently over-penalize for style. Two evaluators with different perspectives -- one focused on correctness/completeness, one focused on systemic impact -- reduce the chance that a consistent bias goes undetected. The isolation is critical: if evaluator B sees evaluator A's critique, it anchors on that critique rather than forming an independent judgment.

**The two perspectives:**
- The **basic evaluator** asks: "Is this correct, complete, and specific?" It scores along correctness, completeness, specificity, and architecture fit.
- The **diffusion evaluator** asks: "What are the second-order consequences?" It scores along caller impact, maintenance debt, emergent behaviour, and rollback safety.

**Acceptance criteria:**
- Both evaluators receive identical input (same subject, same context, same mode header)
- Both evaluators run concurrently; neither blocks on the other's result
- If one evaluator fails (network error, malformed response), the other is explicitly cancelled rather than left as an orphaned background task consuming API quota
- The combined score weights basic at 60% and diffusion at 40%, clamped to [0, 10]
- Both individual scores must independently be within [0, 10]; an out-of-range individual score is a hard error, not silently clamped

---

## R-02: Score extraction resilience

The system must reliably extract a numeric score from free-form LLM output, even when the LLM deviates from the requested format.

**Why:** LLMs are unreliable formatters. The prompt says "write SCORE: 7.5 on its own line" but the LLM might write "Final Score = 7.5", embed it in a markdown code block, include arithmetic like "SCORE = (7*0.4)+(6*0.3)+(5*0.2)+(8*0.1) = 6.4", or place it mid-paragraph. The system cannot fail every time an LLM gets creative with formatting.

**Extraction strategy (ordered by reliability):**
1. Line-anchored match: `SCORE: N` at the start of its own line. When multiple such lines exist, the last one wins (the final authoritative score, not intermediate arithmetic).
2. Unanchored match: `SCORE: N` anywhere in the text, case-insensitive.
3. Loose fallback: any pattern matching `SCORE[=:\s]+N`.

**Acceptance criteria:**
- Scores inside markdown code blocks (triple backticks) are ignored -- they are examples or arithmetic, not the final score
- Scores inside inline code spans (single backticks) are ignored
- When the line-anchored pattern finds multiple matches, the last match is used (the LLM often shows intermediate calculations before the final score)
- When no score is found by any strategy, the system returns 0.0 and logs a warning (not an exception -- a missing score should degrade gracefully, not crash the pipeline)
- Extracted scores outside [0, 10] are clamped and logged as warnings

---

## R-03: Calibration anchoring

The system must detect and flag scores that lack calibration justification, especially at the extremes.

**Why:** Scores drift over time. Without anchoring, evaluators trend toward the comfortable middle (everything gets 6-7) or toward inflation (everything gets 8-9 because the LLM wants to be encouraging). Calibration anchors in the prompt define what each score level means concretely. When an evaluator gives an extreme score, it should reference the anchor language that justifies it.

**Specific calibration guards:**

- **Anti-inflation:** A score of 9.5+ for a debate (text-only) round is suspicious. Text proposals rarely deserve near-perfect scores because they haven't been tested. The system flags this with a specific warning about what would justify 9.5+.
- **Anti-deflation:** A score of 3 or below for an implement round requires evidence of a concrete defect. The system flags when a low score might reflect "incomplete" rather than "broken."
- **Perfect score guard:** A claimed 10.0 triggers a check that every criterion is satisfied. A claimed 0.0 triggers a check that the response is truly absent, not just poor.
- **Anchor keyword validation:** When a score is at the extremes (below 1.5 or above 8.5), the system checks whether the analysis text references appropriate calibration language. A score of 9.5 without words like "comprehensive," "well-tested," or "specific" is flagged as potentially miscalibrated.

**Acceptance criteria:**
- Calibration warnings are advisory (WARNING prefix), not hard failures -- they inform the operator, they do not block the pipeline. **Behavioral note:** score-justification consistency checks (from `validate_calibration_anchors()`) currently produce messages without a "WARNING:" prefix, which causes them to be treated as hard validation failures by `validate_evaluator_output` despite the advisory intent
- The system detects whether calibration anchor phrases from the prompt appear in the evaluator's output (simple keyword presence, not semantic understanding)
- Extreme scores with analysis sections shorter than 15 words are flagged -- a one-sentence justification is not sufficient for an extreme claim
- Score-justification consistency is checked: a low score accompanied by predominantly positive language ("good," "excellent," "great") is flagged as contradictory, and vice versa
- Calibration warnings are generated sparingly to avoid noise

---

## R-04: Mode-aware evaluation

The system must adjust its evaluation criteria based on what kind of output the cycle produced: a text proposal, an executed code change, or a reasoning-only exploration.

**Why:** Judging a plan by the same criteria as executed code is unfair in both directions. A plan cannot have syntax errors (so penalizing for "no tests" is wrong), but a plan can be vague (so requiring specific file/function references is right). Conversely, executed code must actually work, so "the approach seems reasonable" is not enough -- the evaluator needs to check whether tests pass and imports resolve.

**Three modes:**

- **Debate mode:** Evaluates text proposals. Focuses on reasoning quality and specificity. Does NOT penalize for lack of tool calls or execution results. Asks: "Would this plan work if implemented? Does it name concrete files and functions?"
- **Implement mode:** Evaluates executed code changes. Checks correctness, syntax, test results, tool call success/failure. Penalizes missing tests, syntax errors, broken functionality. The proposal text is context only; the actual code state is what matters.
- **Reasoning mode:** Evaluates cycles that produced no code changes. Asks entirely different questions: Did the agent explore? Is the "nothing to change" conclusion backed by evidence? Did it identify new directions? Penalizes empty repetition -- cycles that re-state "mission complete" with no new information.

**Acceptance criteria:**
- Every evaluation request specifies a mode; the mode is prepended as a header to the evaluator's input so the LLM knows which rubric to apply
- Debate and implement modes use the same evaluator prompt structure (basic + diffusion) but with different emphasis via the mode header
- Reasoning mode uses entirely different system prompts with different evaluation dimensions (exploration thoroughness, judgment quality, direction discovery, information density instead of correctness/completeness/specificity/architecture)
- The mode header is concise -- calibration anchors, scoring guidance, and output format are already in the system prompt and should not be duplicated

---

## R-05: Output structure validation

The system must validate that evaluator responses follow the expected structure before extracting scores and feedback.

**Why:** A malformed evaluator response can corrupt scoring in subtle ways. If the ANALYSIS section is missing, the structured feedback extraction silently returns empty results, and the system records a score with no explanation. If the SCORE line is inside a code block (the LLM was showing an example), the system extracts a fake score. Validation catches these cases before they propagate.

**What is validated:**

- **Required sections:** ANALYSIS, TOP DEFECT (basic) or KEY RISK (diffusion), and SCORE must all be present. Missing SCORE is a hard error; other missing sections are advisory warnings. **Note:** reasoning-mode basic uses "TOP ISSUE:" as its diagnostic section header (not "TOP DEFECT:"). However, the validator currently checks for "TOP DEFECT:" regardless of mode, creating a mismatch that always triggers a warning for reasoning-mode basic output.
- **Score format:** The SCORE line must match `SCORE: X.X` format. It should be the last line of output for reliable parsing.
- **Score-in-code-block detection:** A state machine tracks backtick nesting (fenced blocks and inline spans) to detect whether a SCORE line is inside a code block. Scores inside code blocks are flagged.
- **Analysis structure:** Each dimension line should follow the `A. Dimension: N -- description` format. Missing dimensions are warned about but do not fail validation.
- **Defect/risk concreteness:** TOP DEFECT and KEY RISK entries must reference `file::function`, not vague descriptions. Path traversal attempts in file references are detected and rejected.
- **Feedback actionability:** ACTIONABLE FEEDBACK items must be numbered and should contain concrete file/function references.

**Acceptance criteria:**
- Validation returns a tuple of (is_valid, issues_list); only non-WARNING issues make the output invalid
- A response can have multiple warnings and still be valid -- warnings are for operator awareness, not pipeline control
- The SCORE-in-code-block check uses a proper state machine that tracks fenced blocks (triple backticks) and inline spans (single backticks) independently
- Mode-specific validation: debate mode should mention "text proposal" or "planning round"; implement mode should mention "executed code" or "code state". **Behavioral note:** mode-specific content checks produce messages without a "WARNING:" prefix, which causes them to be treated as hard validation failures by `validate_evaluator_output` despite their intent as content guidance checks
- Token budget warning: outputs longer than ~2000 tokens trigger an advisory warning about potential truncation

---

## R-06: Structured feedback extraction

The system must extract actionable information from evaluator output, not just the score.

**Why:** A score of 5.2 tells the system "this is mediocre." But the system needs to know WHY it is mediocre to improve in the next cycle. Structured feedback -- the top defect, the dimension-by-dimension analysis, the concrete improvement suggestion -- becomes the input to the next cycle's prompt.

**What is extracted:**
- The numeric score
- The delta comparison against the prior best (if present)
- Per-dimension analysis scores (correctness, completeness, etc.)
- The top defect or key risk (the single most critical issue)
- Numbered actionable feedback items (stripped of bullet/number prefixes for clean downstream use)
- The "what would make this 10/10" improvement suggestion
- Whether calibration anchor phrases were detected in the output
- Any validation warnings generated during extraction

**Acceptance criteria:**
- Extraction runs validation first; if hard validation errors exist, extraction returns an error result rather than partially-extracted garbage
- Validation warnings are preserved in the result but do not prevent extraction
- Feedback items are stripped of leading numbering (`1.`, `2)`, `-`, `*`) for uniform downstream consumption
- The improvement suggestion is omitted if it says "already perfect" -- this is a non-suggestion
- The defect field is omitted if it says "none" -- this indicates no defect was found
- Multiple analysis line formats are supported: `A. Correctness: 8.5` and `Correctness: 8.5` are both parsed

---

## R-07: Weighted scoring with explicit arithmetic

Each evaluator must show its scoring arithmetic, and the system must verify that the final score is consistent with the dimension scores.

**Why:** "I give this a 7" with no breakdown is unauditable. Requiring explicit arithmetic (`SCORE = (A * 0.4) + (B * 0.3) + (C * 0.2) + (D * 0.1) - penalties = 6.4`) forces the evaluator to justify its score through its components. It also makes score inflation visible: if every dimension is 6 but the final score is 8, something is wrong.

**Basic evaluator weights:** Correctness 40%, Completeness 30%, Specificity 20%, Architecture fit 10%.

**Diffusion evaluator weights:** Caller impact 35%, Maintenance debt 30%, Emergent behaviour 20%, Rollback safety 15%.

**Reasoning mode uses different dimensions entirely:** Exploration thoroughness 35%, Judgment quality 30%, Direction discovery 20%, Information density 15% (basic); Stagnation risk 35%, Opportunity cost 30%, Strategic value 20%, Trajectory health 15% (diffusion).

**Acceptance criteria:**
- The prompt requires evaluators to show arithmetic in the format `SCORE = (A * weight) + ... - penalties = N`
- Penalties are defined per evaluator type: basic penalizes for missing concrete references (-3), missing required sections (-2), and static analysis errors (-1 each); diffusion penalizes for static analysis errors (-1 each)
- Penalties have a floor of 0 (the score cannot go negative from penalties alone)
- Anti-inflation and anti-deflation rules are embedded in the prompt: scores of 9+ require explicit justification; scores below 4 require evidence of concrete defects

---

## R-08: Prior round comparison

When a prior best score exists, each evaluator must compare the current output against it on every dimension before scoring.

**Why:** Without comparison, evaluators judge each cycle in isolation. This means a cycle that introduces a regression (re-breaks something that was previously fixed) gets scored purely on its own merits, missing the fact that it went backwards. The delta comparison forces the evaluator to notice regressions and penalize them.

**Acceptance criteria:**
- The delta section is omitted entirely on round 1 (no prior to compare against)
- Each dimension is marked IMPROVED, REGRESSED, or UNCHANGED with a one-line reason
- A proposal that repeats a known defect from the prior best's TOP DEFECT loses an additional point on Correctness (basic) or Caller Impact (diffusion)
- The delta comparison is structural, not numeric -- the evaluator compares what was said, not just whether the number went up

---

## R-09: Critical range fractional scoring

Scores in the 4.0-7.0 range must use fractional values (e.g., 5.5, 6.5) with defined decision gates at each half-point.

**Why:** The difference between "names specific files but implementation is absent" (4.5) and "names specific files and main path works with gaps" (5.0) is the difference between "talked about fixing it" and "partially fixed it." Integer-only scoring in this range compresses meaningfully different quality levels into the same bucket, destroying the signal the system needs to iterate.

**Decision tree for the basic evaluator:**
1. Does the output name specific files/functions? NO -> score at most 4.0; YES -> at least 4.5
2. Does the output achieve major functionality? NO -> at most 5.5; YES -> at least 6.0
3. Does the output handle edge cases with testability evidence? NO -> at most 6.5; YES -> at least 7.0
4. Is the core goal fully achieved and ready for code review? NO -> at most 7.5; YES -> at least 8.0

Half-point scores (4.5, 5.5, 6.5, 7.5) are explicitly for borderline cases where the evaluator can cite evidence for BOTH the YES and NO conditions of a gate.

**Acceptance criteria:**
- The prompt defines concrete examples at each half-point level so evaluators have anchors, not just abstract descriptions
- The system logs advisory guidance for scores in the 4.0-7.0 range describing what each sub-range means
- Fractional scores are preserved through the entire pipeline (no rounding to integers at any intermediate step)
