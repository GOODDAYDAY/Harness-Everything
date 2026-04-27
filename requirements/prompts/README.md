# Prompts Domain

Status: **Active**

Prompt templates that drive the evaluation and meta-review subsystems. Two source files, four system prompts, three mode headers, one user template.

Source files:
- `harness/prompts/dual_evaluator.py` (274 lines)
- `harness/prompts/agent_meta_review.py` (78 lines)

---

## PRO-01: Dual Evaluator System Prompts

Source: `harness/prompts/dual_evaluator.py`

The dual evaluator runs two LLM calls in parallel with different system prompts. The "basic" evaluator assesses direct quality; the "diffusion" evaluator assesses second-order effects. Neither sees the other's output.

### PRO-01a: BASIC_SYSTEM (code/proposal quality assessment)

**Role**: Meticulous code and proposal reviewer performing structured quality assessment. Evaluates correctness, completeness, and specificity. Looks for concrete defects, not stylistic preferences or hypothetical risks.

**Scoring guide**: 0-10 scale with half-point granularity at key thresholds:
- 0: Broken/dangerous
- 1: Wrong approach, complete rewrite needed
- 2: Trivial case only, major requirements missed
- 3: Partially correct, fails realistic tests
- 4: Right direction but generic, no file/function cited
- 4.5: File/function named BUT implementation absent
- 5: Named + specific, main path works with gaps
- 5.5: Major functionality present, completeness/edges missing
- 6: Names code entities, working example, edge cases missing
- 6.5: Mostly complete, 1-2 significant gaps
- 7: Works with only minor issues, no test assertions
- 7.5: Fully achieved, minor polish remains
- 8: Specific + testable, passes code review
- 9: Correct + tested for main scenarios
- 10: Correct + tested + measurable

**Critical range decision tree** (four gates, evaluated in order):
1. Names specific files/functions? NO -> score <= 4.0; YES -> score >= 4.5
2. Achieves MAJOR functionality? NO -> score <= 5.5; YES -> score >= 6.0
3. Handles EDGE CASES with testability evidence? NO -> score <= 6.5; YES -> score >= 7.0
4. Core goal FULLY achieved, ready for code review? NO -> score <= 7.5; YES -> score >= 8.0

Half-point scores (`.5`) require evidence for BOTH the YES and NO conditions of the gate.

**Anti-inflation rule**: Scores of 9 or 10 require explicit justification stating what specifically makes it near-perfect. Without a concrete reason, max score is 8.

**Anti-deflation rule**: Scores below 4 require evidence of a concrete defect (specific wrong line, missing function, or broken assumption). Do NOT deduct heavily for style or missing tests alone.

**Evaluation dimensions** (weighted average):
| Dimension | Weight | Focus |
|-----------|--------|-------|
| A. Correctness | 40% | Logic errors, wrong assumptions, missing guard clauses, incorrect data flow, off-by-one errors. Must quote the specific defective construct. |
| B. Completeness | 30% | Cross-reference each clause of the task against the proposal. Gap requiring follow-up PR scores at most 6. |
| C. Specificity | 20% | References to concrete code entities (function names, class names, file paths). Vague proposals score <= 4 regardless. |
| D. Architecture fit | 10% | Existing patterns, naming conventions, module boundaries. Score <= 5 if introducing dependency inversion. |

**Penalties** (applied after weighted average, minimum score 0):
- -3 if no concrete function/class/file name from source context is cited
- -2 if a required section (per the task) is entirely absent
- -1 per static analysis ERROR finding already surfaced in the prompt context

**Score arithmetic formula**:
```
SCORE = (A * 0.40) + (B * 0.30) + (C * 0.20) + (D * 0.10) - penalties
```

**Prior round delta**: When a "Prior Best" is present in context, the evaluator compares each dimension as IMPROVED/REGRESSED/UNCHANGED. A proposal that repeats a known defect flagged in the prior best's TOP DEFECT loses 1 extra point on Correctness.

**Required output sections** (exact structure, each on its own line):
1. `DELTA VS PRIOR BEST:` (only when prior best exists; omitted on round 1)
   - Per-dimension delta lines: `Δ Correctness:`, `Δ Completeness:`, `Δ Specificity:`, `Δ Architecture:`
2. `ANALYSIS:` with sub-items A-D, Penalties, Weighted arithmetic
3. `TOP DEFECT:` formatted as `FILE::function -- problem description and what a correct fix looks like`; or `none` if score >= 9
4. `ACTIONABLE FEEDBACK:` numbered list (1-3 items), format: `file.py::function -- exact change needed`
5. `WHAT WOULD MAKE THIS 10/10:` one concrete sentence naming exact change (file, function, behaviour); or `already perfect` if score = 10
6. `SCORE:` final value, rounded to one decimal place

### PRO-01b: DIFFUSION_SYSTEM (second-order effects assessment)

**Role**: Systems-thinking analyst evaluating second-order effects. Assumes the proposal is correctly implemented and runs without errors. Assesses consequences beyond the directly touched code: caller impact, maintenance cost, emergent behaviour, rollback safety.

**Scoring guide**: 0-10 scale (inverse -- higher is better / less impact):
- 0: Catastrophic -- irreversible or systemically destabilising
- 1: Near-catastrophic -- extreme cascade, no clear recovery
- 2: Dangerous -- breaks unrelated functionality with no mitigation
- 3: Severe -- breaks 5-9 callers or corrupts shared state
- 4: Concerning -- significant cascade effects needing explicit mitigation
- 5: Moderate-concerning -- real cross-module impact, manageable
- 6: Moderate -- some callers affected but bounded
- 7: Low-moderate -- minor cross-module ripple, easy to address
- 8: Minor -- trivial ripple effects easily addressed
- 9: Near-negligible -- contained to one module with trivial upstream acknowledgement
- 10: Negligible -- zero maintenance overhead, trivial rollback

**Critical range decision tree** (four gates):
1. Modifies public API used by >= 3 callers WITHOUT shim/adapter? YES -> score <= 4; NO -> continue
2. Requires coordinated changes across 2+ files to be safe? YES -> score <= 6; NO -> continue
3. Rollback straightforward WITHOUT data-migration/format-version bump? YES -> score >= 5; NO -> score <= 5
4. All emergent effects provably bounded to a single module? YES -> score >= 7; NO -> score <= 7

**Anti-inflation rule**: Scores of 9 or 10 require explicit justification. A change modifying a public API or shared data structure almost never scores 10 on Caller Impact.

**Anti-deflation rule**: Do NOT manufacture negative findings. If second-order effects are genuinely minimal, say so clearly and score high.

**Evaluation dimensions** (weighted average):
| Dimension | Weight | Focus |
|-----------|--------|-------|
| A. Caller impact | 35% | How the change affects code that calls or depends on modified components. Name specific callers. |
| B. Maintenance debt | 30% | Ongoing cost: test surface, documentation burden, migration effort, coupling. Name specific files/patterns. |
| C. Emergent behaviour | 20% | Non-obvious behaviours at scale, under concurrent load, or at boundary conditions. |
| D. Rollback safety | 15% | Difficulty of revert: persisted format changes, protocol changes, database schema changes. |

**Penalties**:
- -1 per static analysis ERROR finding already surfaced in the prompt context

**Score arithmetic formula**:
```
SCORE = (A * 0.35) + (B * 0.30) + (C * 0.20) + (D * 0.15) - penalties
```

**Prior round delta**: Same as BASIC_SYSTEM. Reintroducing a risk identified as KEY RISK in prior best loses 1 extra point on Caller Impact.

**Required output sections**:
1. `DELTA VS PRIOR BEST:` (only when prior best exists)
   - Per-dimension: `Δ Caller impact:`, `Δ Maintenance debt:`, `Δ Emergent behaviour:`, `Δ Rollback safety:`
2. `ANALYSIS:` with sub-items A-D, Penalties, Weighted arithmetic
3. `KEY RISK:` formatted as `FILE::function -- scenario description -> concrete mitigation step`; or `none` if score >= 9
4. `ACTIONABLE MITIGATIONS:` numbered list (1-3 items), format: `file.py::function -- exact guard needed`
5. `WHAT WOULD MAKE THIS 10/10:` one concrete sentence; or `already perfect` if score = 10
6. `SCORE:` final value, rounded to one decimal place

### PRO-01c: REASONING_BASIC_SYSTEM (no-code-change quality assessment)

**Role**: Evaluating an autonomous coding agent's cycle that produced NO code changes. Assesses exploration, reasoning, and decision quality.

**Scoring guide**: 0-10 scale:
- 0: Agent did nothing
- 1-2: Empty cycle -- declared "mission complete" with no evidence
- 3: Minimal effort -- read one file, vague statement
- 4: Some exploration but no real analysis
- 5: Reasonable exploration, weak conclusion
- 6: Solid exploration with supported conclusion
- 7: Thorough exploration, good notes
- 8: Excellent -- deep exploration, well-reasoned conclusion, concrete next directions
- 9-10: Exceptional -- non-obvious insights, high-value notes, specific new direction with evidence

**Evaluation dimensions** (weighted average):
| Dimension | Weight | Focus |
|-----------|--------|-------|
| A. Exploration thoroughness | 35% | Files read, searches performed, tests run. Zero files read = 0. |
| B. Judgment quality | 30% | Is the conclusion well-reasoned? Conclusion without evidence scores <= 3. |
| C. Direction discovery | 20% | Identified valuable next steps, TODOs, missing tests, technical debt. |
| D. Information density | 15% | NEW information vs repetition. Repeating "mission complete" = 0. |

**Penalties**:
- -3 if output is essentially identical to previous cycle's (stale repetition)
- -2 if no tool calls were made (pure text generation without exploration)

**Score arithmetic**:
```
SCORE = (A * 0.35) + (B * 0.30) + (C * 0.20) + (D * 0.15) - penalties
```

**Required output sections**:
1. `ANALYSIS:` with sub-items A-D, Penalties, Weighted arithmetic
2. `TOP ISSUE:` the single most important thing the agent should have done differently
3. `ACTIONABLE FEEDBACK:` numbered list (1-2 items)
4. `SCORE:` final value, rounded to one decimal place

### PRO-01d: REASONING_DIFFUSION_SYSTEM (no-code-change trajectory assessment)

**Role**: Systems-thinking analyst evaluating an autonomous agent's decision NOT to change code. Assesses whether inaction is justified and what second-order effects the non-action has on project trajectory.

**Scoring guide**: 0-10 scale:
- 0: Agent is stuck in a loop
- 1-2: Agent is avoiding work -- clear improvements exist
- 3-4: Inaction partially justified but missed obvious opportunities
- 5-6: Reasonable pause -- explored, found little to do, decent notes
- 7-8: Good judgment -- correctly identified current direction is done, pivoted exploration
- 9-10: Excellent strategic thinking -- non-obvious opportunities or correctly deprioritized low-value changes

**Evaluation dimensions** (weighted average):
| Dimension | Weight | Focus |
|-----------|--------|-------|
| A. Stagnation risk | 35% | Is the agent entering a repetitive loop? Single empty cycle after major work = fine. Three consecutive empty = alarming (score <= 4). |
| B. Opportunity cost | 30% | What improvements exist that the agent is not pursuing? (test coverage, error handling, docs, performance, code quality) |
| C. Strategic value | 20% | Even without code changes, did exploration add value? Validate codebase health, discover constraints? |
| D. Trajectory health | 15% | Is the project on a good trajectory? Will the next cycle be productive? |

**Penalties**:
- -2 if this is the 3rd+ consecutive cycle with no code changes
- -1 if the agent's notes don't mention any concrete next steps

**Score arithmetic**:
```
SCORE = (A * 0.35) + (B * 0.30) + (C * 0.20) + (D * 0.15) - penalties
```

**Required output sections**:
1. `ANALYSIS:` with sub-items A-D, Penalties, Weighted arithmetic
2. `KEY RISK:` the biggest risk of continued inaction
3. `ACTIONABLE MITIGATIONS:` numbered list (1-2 items)
4. `SCORE:` final value, rounded to one decimal place

---

## PRO-02: Evaluation Mode Headers

Source: `harness/evaluation/dual_evaluator.py`, lines 81-105 (`_MODE_HEADERS` dict)

Mode headers are prepended to the user message (before the subject and source context) so the evaluator LLM knows what type of content it is reviewing. The mode is passed to `DualEvaluator.evaluate()` and selects both the system prompt variant and the mode header.

### PRO-02a: `debate` mode header

Used when evaluating text proposals (plans / recommendations), NOT executed code.

Header text:
```
## EVALUATION MODE: DEBATE (TEXT PROPOSAL)
You are reviewing a **text proposal** (plan / recommendation), NOT executed code.
- Evaluate reasoning quality and specificity of proposed changes
- Do NOT penalize for lack of tool calls or execution results
- Assess whether the plan names concrete files/functions and would work if implemented
```

System prompts used: `BASIC_SYSTEM` + `DIFFUSION_SYSTEM`

### PRO-02b: `implement` mode header

Used when evaluating executed code changes, NOT proposals.

Header text:
```
## EVALUATION MODE: IMPLEMENT (EXECUTED CODE)
You are reviewing an **executed code change**, NOT a proposal.
- Evaluate the actual code state; the proposal text is context only
- Check correctness, syntax, test results, and tool call success/failure
- Penalize missing tests, syntax errors, and broken functionality
```

System prompts used: `BASIC_SYSTEM` + `DIFFUSION_SYSTEM`

### PRO-02c: `reasoning` mode header

Used when a cycle produced no code changes. Evaluates the agent's exploration, reasoning, and decision quality.

Header text:
```
## EVALUATION MODE: REASONING (NO CODE CHANGES)
This cycle produced **no code changes**. You are evaluating the agent's
exploration, reasoning, and decision quality -- NOT code.
- Did the agent actively explore the codebase (read files, search, run tests)?
- Is the conclusion (e.g. 'nothing to change') backed by evidence?
- Did the agent identify new directions or leave actionable notes for the next cycle?
- Penalize empty repetition: cycles that just re-state 'mission complete' with no new information
```

System prompts used: `REASONING_BASIC_SYSTEM` + `REASONING_DIFFUSION_SYSTEM`

### Mode selection logic

In `DualEvaluator.evaluate()` (line 925-934):
- If `mode == "reasoning"`: uses `REASONING_BASIC_SYSTEM` / `REASONING_DIFFUSION_SYSTEM`
- Otherwise (including `mode == "debate"` and `mode == "implement"`): uses `BASIC_SYSTEM` / `DIFFUSION_SYSTEM`
- Mode header is looked up via `_MODE_HEADERS.get(mode, _MODE_HEADERS["debate"])` -- unknown modes fall back to the `debate` header

### User message structure

After the mode header, the user message is assembled as:
```
{mode_header}## Subject to Evaluate

{subject}

## Source Context

{context}
```

Parameters `subject` and `context` are passed by the caller of `DualEvaluator.evaluate()`.

---

## PRO-03: Agent Meta-Review Prompt

Source: `harness/prompts/agent_meta_review.py` (78 lines)

The meta-review runs every N cycles (configured via `meta_review_interval` in `AgentConfig`). It analyses score trends and git history, producing strategic direction guidance that gets injected into subsequent cycles' system prompts.

### PRO-03a: AGENT_META_REVIEW_SYSTEM

**Role**: Strategic advisor analysing an autonomous coding agent's recent performance. Receives evaluation scores, git history delta, and the agent's own notes.

**Guidelines** (embedded in the system prompt):
- Be concrete -- name files, functions, and specific metrics
- Focus on actionable direction, not generic advice
- If scores are consistently high (>= 8): acknowledge success and suggest stretch goals or new focus areas
- If scores are dropping: diagnose root cause and suggest specific corrective action
- If agent's notes show repeated cycles with no code changes: the current direction is EXHAUSTED; Direction Adjustment MUST propose entirely new focus areas (scan git history and notes for unexplored parts)
- If no evaluation scores are available: focus analysis on git delta and agent notes instead
- Output limit: **under 500 words** (the agent reads this every cycle until the next review)

### PRO-03b: AGENT_META_REVIEW_USER

A user-message template with three `$`-prefixed template variables substituted via `str.replace()`:

**Template variables**:

| Variable | Format | Size limit | Fallback |
|----------|--------|------------|----------|
| `$score_history` | Markdown table with columns: Cycle, Basic, Diffusion, Combined. Generated by `format_score_history()`. Shows last 20 entries. | Last 20 cycles | `(no scores recorded yet)` |
| `$git_delta` | Output of `agent_git.get_review_git_delta()` showing commits since last review hash | N/A | N/A |
| `$current_notes` | Agent's current notes text | Truncated to last 3000 chars if longer (logged at debug level) | N/A |

**Score history table format** (from `format_score_history()` in `agent_eval.py`):
```
| Cycle | Basic | Diffusion | Combined |
|-------|-------|-----------|----------|
| 1     | 7.0   | 8.0       | 7.5      |
```

**Required output sections** (six, exact headings):
1. `### Progress Summary` -- concrete deliverables (files changed, features added, bugs fixed)
2. `### Score Trend` -- improving, declining, or plateauing; call out consistently weak dimensions
3. `### Recurring Issues` -- mistakes or anti-patterns that keep appearing; name specific files or patterns
4. `### What Worked` -- approaches that produced highest scores
5. `### Gaps` -- important work NOT being done; areas of codebase being ignored
6. `### Direction Adjustment` -- concrete instructions for the next 3-5 cycles; specific directives like "Focus on X before moving to Y", "Stop doing Z", "The weakest dimension is A -- prioritise it by doing B"
