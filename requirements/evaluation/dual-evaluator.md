# Dual Evaluator

Two independent scoring perspectives evaluate every submission in isolation. Neither sees the other's output, preventing groupthink. Their scores combine into a single quality signal through weighted averaging.

---

## Score Independence

### US-01: As the evaluator, I need the two scoring perspectives to run in parallel without seeing each other's output, so that their assessments remain independent and free from mutual influence

If one perspective could read the other's critique before scoring, it would anchor on that opinion. Running them concurrently with separate system instructions ensures genuine independence.

#### Acceptance Criteria
- Given a submission to evaluate, when both perspectives are invoked, then they execute concurrently with identical input content but different review instructions
- Given that one perspective fails or raises an error, then the other perspective is cancelled rather than left running in the background
- Given successful completion of both perspectives, then each produces its own score and critique independently

---

## Score Extraction

### US-02: As the evaluator, I need to reliably extract a numeric score from free-text evaluator output, so that the final score is determined by the evaluator's stated verdict rather than by formatting artifacts

Evaluators produce natural-language output with a score embedded in it. The extraction must handle formatting variations (different casing, delimiters, inline math) while preferring the most authoritative placement -- the final score stated at the end of the output.

#### Acceptance Criteria
- Given evaluator output with a score on its own line at the end, then that score is extracted as the authoritative value
- Given evaluator output with multiple score mentions (e.g., intermediate arithmetic and a final verdict), then the last line-anchored mention is used
- Given evaluator output where no line-anchored score exists but an inline mention does, then the inline mention is used as a fallback
- Given evaluator output with no recognizable score at all, then a default minimum score is returned and a warning is logged
- Given evaluator output where the score appears inside a code block or inline code span, then that occurrence is ignored to avoid false positives

### US-03: As the evaluator, I need extracted scores to be constrained to the valid scoring range, so that parsing artifacts or evaluator mistakes do not produce out-of-range values that break downstream logic

A raw extracted value outside the defined scale would be meaningless and could distort aggregations. Clamping ensures every score is usable.

#### Acceptance Criteria
- Given a raw extracted value above the maximum of the scale, then it is clamped to the maximum and a warning is logged
- Given a raw extracted value below the minimum of the scale, then it is clamped to the minimum and a warning is logged
- Given a raw extracted value within the valid range, then it is returned unchanged

---

## Calibration

### US-04: As the evaluator, I need scores near the extremes to be accompanied by calibration evidence, so that perfect or near-zero scores are justified by specific criteria rather than given casually

Extreme scores carry outsized influence on decision-making. Without explicit justification, they are likely miscalibrated. The system should warn when extreme scores lack the language expected from a well-calibrated evaluation.

#### Acceptance Criteria
- Given an extreme low score, when the analysis section does not reference any calibration anchor language appropriate to that range, then a warning is produced
- Given an extreme high score, when the analysis section does not reference any calibration anchor language appropriate to that range, then a warning is produced
- Given a score in the non-extreme range, then no calibration anchor check is performed
- Given an extreme score with a very brief justification, then an advisory warning is produced recommending more detailed analysis
- Given an extreme score where the tone of the analysis contradicts the score direction (e.g., positive language with a very low score), then an inconsistency warning is produced

### US-05: As the evaluator, I need targeted warnings when a score is suspicious relative to its context, so that obvious miscalibrations are flagged before the score is consumed

Certain score-context combinations are inherently suspect: a near-perfect score for a text-only proposal, or a near-zero score for executed code that may simply be incomplete rather than broken. These deserve targeted, concise warnings.

#### Acceptance Criteria
- Given a score outside the valid range, then a warning is produced indicating a likely parsing error
- Given a near-perfect score in a text-proposal evaluation mode, then a warning is produced advising that such scores should be reserved for exceptionally specific and complete proposals
- Given a very low score in an executed-code evaluation mode, then a warning is produced asking to confirm that the code is truly broken rather than just incomplete
- Given a score at the absolute maximum, then a warning is produced requesting confirmation that every criterion is fully satisfied
- Given a score at the absolute minimum, then a warning is produced requesting confirmation that the response is entirely absent or meaningless

---

## Mode-Aware Evaluation

### US-06: As a cycle, I need the evaluation to adapt its rubric based on what kind of work the cycle produced, so that text proposals are judged on reasoning quality and executed code is judged on correctness

A text proposal and a code change are fundamentally different artifacts. Judging a proposal by whether tests pass, or judging code by whether the reasoning is elegant, produces misleading scores. Each evaluation mode must apply the appropriate lens.

#### Acceptance Criteria
- Given a text-proposal cycle, when evaluation begins, then the evaluator receives instructions to assess reasoning quality and specificity of proposed changes, and not to penalize the absence of execution results
- Given an executed-code cycle, when evaluation begins, then the evaluator receives instructions to assess the actual code state, check correctness and test results, and penalize syntax errors and broken functionality
- Given a reasoning-only cycle with no code changes, when evaluation begins, then the evaluator receives instructions to assess exploration thoroughness, the quality of conclusions, and whether actionable notes were left for future cycles
- Given any evaluation mode, when the mode is unrecognized, then a default mode is applied so evaluation does not fail

---

## Output Validation

### US-07: As the evaluator, I need the output of each scoring perspective to be structurally validated, so that malformed output is detected before scores are consumed

Each scoring perspective must produce output in a defined structure (analysis section, defect identification, score line). Missing or malformed sections indicate the evaluator did not follow its instructions, which undermines trust in the score.

#### Acceptance Criteria
- Given evaluator output missing the score line, then a hard validation error is produced and the output is marked invalid
- Given evaluator output missing advisory sections (analysis, defect identification), then warnings are produced but the output remains valid
- Given evaluator output where the score line is malformed (not matching the expected format), then a validation error is produced
- Given evaluator output where the score line is not the last line, then a warning is produced advising that final placement is preferred for reliable parsing
- Given evaluator output that exceeds a reasonable length, then a warning is produced advising truncation
- Given evaluator output for a debate/text-proposal mode, when validation runs, then the validator checks that the output contains mode-appropriate terminology (e.g., "text proposal"); given evaluator output for an implement/executed-code mode, when validation runs, then the validator checks for terminology such as "executed code"

### US-08: As the evaluator, I need the defect and feedback sections to reference concrete code locations, so that findings are actionable rather than vague

A finding like "there is a bug" is not actionable. Findings that name a specific file and function give the agent a precise target. The validator checks for these references and warns when they are absent.

#### Acceptance Criteria
- Given a defect section that names a file and function, then no reference warning is produced
- Given a defect section with free text but no file-and-function reference, then a validation error is produced
- Given a defect section containing path traversal patterns, then a validation error is produced for security
- Given a feedback section with numbered items, when none reference a specific file or function, then a warning is produced
- Given a feedback section without numbered items, then a validation error is produced requesting structured feedback

---

## Feedback Extraction

### US-09: As a cycle, I need structured feedback extracted from each scoring perspective's free-text output, so that actionable items, defects, and improvement suggestions are available as data rather than buried in prose

The downstream system needs to consume evaluation results programmatically -- routing defects to the agent, tracking improvement suggestions across cycles, and comparing deltas. Extracting these from free text into a structured form enables this.

#### Acceptance Criteria
- Given valid evaluator output, then the extracted result includes the numeric score, a list of actionable feedback items, the top defect (if any), and an improvement suggestion (if any)
- Given evaluator output with a delta-vs-prior section, then only the summary line immediately following the "DELTA VS PRIOR BEST:" header is extracted and included in the result (the per-dimension breakdown is not extracted)
- Given evaluator output with dimension-level analysis scores, then those scores are extracted as a mapping of dimension names to values
- Given evaluator output that fails hard validation, then the extraction returns an error description and does not attempt to parse further
- Given evaluator output with calibration-related language, then the result indicates that calibration anchors were referenced
- Given evaluator output where the improvement suggestion is "already perfect", then no improvement suggestion is included in the result

### US-10: As the agent, I need structured feedback formatted into a readable critique string, so that the evaluation results can be presented as human-readable text for logging and context injection

Raw structured data is not suitable for direct consumption by the agent or for logging. A formatted version that presents score, feedback items, defects, and analysis in a readable layout bridges the gap between structured extraction and human consumption.

#### Acceptance Criteria
- Given a structured feedback result with a score, feedback items, and a defect, then the formatted output presents each in a labeled, readable format
- Given a structured feedback result with dimension-level analysis scores, then each dimension and its score are listed
- Given an empty or absent feedback result, then a fallback message indicating no feedback is returned

---

## Weighted Scoring

### US-11: As a cycle, I need the two independent scores combined into a single value using a fixed weighting, so that the detailed correctness assessment and the system-level impact assessment both contribute to the final quality signal in a predictable ratio

The two scoring perspectives measure different things: one measures whether the work is correct and complete, the other measures whether it is safe and sustainable at the system level. A fixed weighting ensures the combination is deterministic and consistent across all evaluations.

#### Acceptance Criteria
- Given two valid scores from the independent perspectives, then the combined score is a weighted average where the correctness perspective contributes more than the impact perspective
- Given that either individual score is outside the valid range, then a validation error is raised before combination
- Given valid individual scores, then the combined score is clamped to the valid range

---

## Prior Comparison

### US-12: As the evaluator, I need to compare the current submission against the best result from prior rounds, so that regressions are detected and improvements are recognized relative to an established baseline

Without a baseline comparison, each evaluation is in isolation. Comparing against the prior best on each scoring dimension (improved / regressed / unchanged) gives the cycle a trajectory signal and penalizes repeating known defects.

#### Acceptance Criteria
- Given that a prior best result exists in the evaluation context, then the evaluator compares the current submission against it on each scoring dimension and notes the delta direction
- Given that no prior best result exists (first round), then the delta section is omitted entirely
- Given that the current submission repeats a defect that was already identified in the prior best, then the evaluator output reflects an additional penalty for regression on a known issue
- Given a delta section is present, then it contains descriptive comparison text rather than being empty or trivially short
