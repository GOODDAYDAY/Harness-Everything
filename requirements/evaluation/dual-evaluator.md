# Dual Evaluator

Two independent LLM-based evaluators that never see each other's output, preventing groupthink. Scores are combined into a weighted average.

Source: `harness/evaluation/dual_evaluator.py` (1017 lines)

---

## Data Structures

### ScoreItem

```python
@dataclass
class ScoreItem:
    score: float
    critique: str
```

A single evaluator's numeric score and formatted critique string.

### DualScore

```python
@dataclass
class DualScore:
    basic: ScoreItem
    diffusion: ScoreItem
```

**Combined score formula** (`combined` property):

```
weighted_score = 0.6 * basic.score + 0.4 * diffusion.score
result = max(0.0, min(10.0, weighted_score))
```

- Basic evaluator weight: **60%** (detailed correctness evaluation)
- Diffusion evaluator weight: **40%** (system-level impact evaluation)
- Result is clamped to `[0.0, 10.0]`

| # | Given | When | Then |
|---|-------|------|------|
| 1 | `basic.score` is outside `[0.0, 10.0]` | `combined` accessed | raises `ValueError(f"Basic score {self.basic.score} is outside valid range [0.0, 10.0]")` |
| 2 | `diffusion.score` is outside `[0.0, 10.0]` | `combined` accessed | raises `ValueError(f"Diffusion score {self.diffusion.score} is outside valid range [0.0, 10.0]")` |
| 3 | both scores are in range | `combined` accessed | returns `0.6 * basic.score + 0.4 * diffusion.score`, clamped to `[0.0, 10.0]` |

---

## Constants

### Score Boundaries

```python
_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0
```

### Regex Patterns for Score Extraction

| Name | Pattern | Flags | Purpose |
|------|---------|-------|---------|
| `_STRICT_RE` | `r"^\s*SCORE:\s+(\d+(?:\.\d+)?)(?:\s+.*)?$"` | `re.MULTILINE` | Line-anchored `SCORE: N`; preferred format |
| `_STRICT_UNANCHORED_RE` | `r"SCORE:\s+(\d+(?:\.\d+)?)"` | `re.IGNORECASE` | `SCORE: N` anywhere in text |
| `_LOOSE_RE` | `r"SCORE[:\s=]+(\d+(?:\.\d+)?)"` | `re.IGNORECASE` | `SCORE N`, `SCORE=N`, etc. |
| `_INLINE_SCORE_RE` | `` r"`[^`\n]*\bSCORE[^`\n]*`" `` | `re.IGNORECASE` | Inline code spans containing SCORE (suppressed as false positives) |
| `_ENHANCED_RE` | `r"(?:SCORE\|Score\|score)[:\s=]+(\d+(?:\.\d+)?)(?:\s*/\s*10\|\s*\(out of\s*10\)\|\s*of\s*10)?"` | `re.IGNORECASE` | `SCORE: 7.5/10` or `SCORE: 8 (out of 10)` |
| `_FINAL_SCORE_RE` | `r"FINAL\s+SCORE[:\s=]+(\d+(?:\.\d+)?)"` | `re.IGNORECASE` | `FINAL SCORE: N` |

---

## Mode Adaptation Headers

The `_MODE_HEADERS` dictionary maps mode strings to markdown header blocks prepended to every evaluator user message. Three modes are defined:

### `"debate"`

```
## EVALUATION MODE: DEBATE (TEXT PROPOSAL)
You are reviewing a **text proposal** (plan / recommendation), NOT executed code.
- Evaluate reasoning quality and specificity of proposed changes
- Do NOT penalize for lack of tool calls or execution results
- Assess whether the plan names concrete files/functions and would work if implemented
```

### `"implement"`

```
## EVALUATION MODE: IMPLEMENT (EXECUTED CODE)
You are reviewing an **executed code change**, NOT a proposal.
- Evaluate the actual code state; the proposal text is context only
- Check correctness, syntax, test results, and tool call success/failure
- Penalize missing tests, syntax errors, and broken functionality
```

### `"reasoning"`

```
## EVALUATION MODE: REASONING (NO CODE CHANGES)
This cycle produced **no code changes**. You are evaluating the agent's
exploration, reasoning, and decision quality — NOT code.
- Did the agent actively explore the codebase (read files, search, run tests)?
- Is the conclusion (e.g. 'nothing to change') backed by evidence?
- Did the agent identify new directions or leave actionable notes for the next cycle?
- Penalize empty repetition: cycles that just re-state 'mission complete' with no new information
```

---

## `parse_score` Function

```python
def parse_score(
    text: str,
    pattern: str = r"SCORE[=:\s]+(\d+(?:\.\d+)?)",
) -> float:
```

Extracts a numeric score from evaluator output and clamps it to `[0, 10]`.

### Three-Tier Extraction Strategy

| Tier | Method | Detail |
|------|--------|--------|
| 1 | **Strict anchored** | For each line in cleaned text, check if it matches `r'^\s*SCORE:\s+'` (case-insensitive). On that line, find all `_STRICT_UNANCHORED_RE` matches and take the **last** value. Across all matching lines, take the **last** line's value. |
| 2 | **Strict unanchored** | If no anchored match, take the last value from `last_per_line` (the last `_STRICT_UNANCHORED_RE` match on any line). |
| 3 | **Loose fallback** | If neither tier found a match, apply `re.findall(pattern, clean_text, re.IGNORECASE)` using the caller-supplied `pattern` parameter. Takes the **last** match. |

If no match is found at any tier, logs a warning (first 500 chars of text) and returns `0.0`.

### Pre-Processing: Code Block Removal

Before extraction, the function cleans the text:

1. **Fenced code blocks** (```` ``` ````): Iterates lines; tracks `in_code_block` state toggled by lines containing ```` ``` ````. Lines inside code blocks are dropped. Exception: if a line contains both ```` ``` ```` and `SCORE:` (case-insensitive), it is kept.

2. **Inline code spans**: After fenced block removal, applies `_INLINE_SCORE_RE.sub("", clean_text)` to strip inline backtick spans that contain `SCORE` patterns (e.g., `` `SCORE: 5` ``). Only spans containing `SCORE` are stripped to preserve line-anchor status for lines where inline code precedes a real score.

### Score Clamping

```python
clamped = max(_SCORE_MIN, min(_SCORE_MAX, raw))
```

| # | Given | When | Then |
|---|-------|------|------|
| 1 | extracted value < 0.0 | clamping | clamped to `0.0`, warning logged |
| 2 | extracted value > 10.0 | clamping | clamped to `10.0`, warning logged |
| 3 | extracted value within `[0.0, 10.0]` | clamping | returned as-is |
| 4 | no score token found in text | extraction | returns `0.0`, warning logged with first 500 chars of cleaned text |

### Critical Range Discrimination Guidance

When the clamped score falls in `[4.0, 7.0]`, debug-level log messages provide fractional discrimination guidance:

| Range | Guidance |
|-------|----------|
| 4.0-4.4 | Generic approach without specific implementation |
| 4.5-4.9 | Generic approach with some specific elements, but not enough for full 5 |
| 5.0-5.4 | Partial success with specific elements |
| 5.5-5.9 | Specific but incomplete with some edge cases addressed |
| 6.0-6.4 | Specific implementation with gaps |
| 6.5-6.9 | Mostly complete with some testability elements, but not enough for 7 |
| 7.0 | Mostly complete implementation with minor edge cases missing (note: outer guard restricts clamped score to [4.0, 7.0], so only 7.0 reaches this branch) |

If the clamped score is fractional (`clamped % 1 != 0`), an additional debug log requests justification in the evaluator output.

---

## `extract_structured_feedback` Function

```python
def extract_structured_feedback(
    text: str,
    evaluator_type: str = "basic",
    context: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
```

Parses evaluator output text into a structured dictionary.

### Return Fields

The returned dictionary always contains these keys:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `score` | `float \| None` | `None` | Extracted via `_STRICT_RE.findall(text)`, takes the **last** match |
| `delta` | `str \| None` | `None` | Text after `"DELTA VS PRIOR BEST:"` up to the next newline, stripped |
| `analysis` | `dict` | `{}` | Mapping of dimension names to float scores from the `ANALYSIS:` section |
| `defect` | `str \| None` | `None` | Text after defect key up to newline; `None` if text is `"none"` (case-insensitive) |
| `feedback_items` | `list[str]` | `[]` | List of actionable feedback strings, stripped of leading bullets/numbering |
| `improvement_suggestion` | `str \| None` | `None` | Text after `"WHAT WOULD MAKE THIS 10/10:"`; `None` if `"already perfect"` (case-insensitive) |
| `warnings` | `list[str]` | `[]` | Warnings from `validate_evaluator_output` and `validate_score_calibration` |
| `calibration_anchors_used` | `bool` | `False` | `True` if any calibration phrase is detected in text |
| `validation_errors` | `list[str]` | `[]` | Hard validation errors if any |

An additional key `"error"` (str) is set when validation produces hard errors, with the message `"Invalid evaluator output: "` followed by semicolon-joined error strings.

### Processing Steps

| # | Step | Behavior |
|---|------|----------|
| 1 | **Validation** | Calls `validate_evaluator_output(text, evaluator_type)`. Separates issues into errors (no `"WARNING:"` prefix) and warnings (`"WARNING:"` prefix). If hard errors exist, sets `result["error"]` and `result["validation_errors"]`, returns immediately. |
| 2 | **Calibration anchor detection** | Checks for case-insensitive presence of any phrase in the tuple: `"SCORING CALIBRATION"`, `"0-10 scale"`, `"score ≤ 5"`, `"score ≥ 8"`, `"critical failure"`, `"core goal achieved"`, `"risk assessment"`. Sets `calibration_anchors_used` to `True` if any match. |
| 3 | **Delta extraction** | Splits text on `"DELTA VS PRIOR BEST:"` and takes first line of the remainder, stripped. |
| 4 | **Score extraction** | Uses `_STRICT_RE.findall(text)`, takes the last match. If found, also runs `validate_score_calibration` and appends resulting warnings. |
| 5 | **Analysis dimensions** | Splits text on `"ANALYSIS:"`, takes content up to next `"\n\n"`. Tries two regex patterns in order, stops after first produces results: (a) `r"[A-D]\.\s+([^:—\n]+):\s*(\d+(?:\.\d+)?)"` and (b) `r"^([^:\n—]+):\s*(\d+(?:\.\d+)?)"` (with `re.MULTILINE`). |
| 6 | **Defect/risk extraction** | Key depends on evaluator type: `"TOP DEFECT:"` for basic, `"KEY RISK:"` for diffusion. Text after the key up to newline; set to `None` if content equals `"none"` (case-insensitive). |
| 7 | **Feedback items** | Section name: `"ACTIONABLE FEEDBACK:"` for basic, `"ACTIONABLE MITIGATIONS:"` for diffusion. Content is trimmed at the next major section (`"WHAT WOULD MAKE THIS 10/10:"`, `"SCORE:"`, `"FINAL SCORE:"`, `"COMBINED_SCORE:"`). Each non-empty line is stripped of leading numbering (`r"^\s*\d+[.)]\s+"`) or bullets (`r"^\s*[-*•]\s+"`). |
| 8 | **Improvement suggestion** | Text after `"WHAT WOULD MAKE THIS 10/10:"` up to newline. Set to `None` if content equals `"already perfect"` (case-insensitive). |

---

## `format_critique_from_feedback` Function

```python
def format_critique_from_feedback(feedback_dict: dict[str, Any]) -> str
```

Converts the structured feedback dictionary into a human-readable critique string.

| # | Given | When | Then |
|---|-------|------|------|
| 1 | `feedback_dict` is empty or falsy | called | returns `"No feedback available"` |
| 2 | `score` key is not `None` | formatting | adds `"Score: {score:.1f}"` |
| 3 | `feedback_items` is non-empty | formatting | adds `"Feedback:"` header, then each item as `"  \u2022 {item}"` |
| 4 | `improvement_suggestion` is truthy | formatting | adds `"Improvement suggestion: {improvement}"` |
| 5 | `defect` is truthy | formatting | adds `"Critical defect: {defect}"` |
| 6 | `analysis` dict is non-empty | formatting | adds `"Analysis:"` header, then each dimension as `"  \u2022 {dimension}: {float(score_val):.1f}"` |

Parts are joined with `"\n"`.

---

## `validate_score_calibration` Function

```python
def validate_score_calibration(
    score: float,
    evaluator_type: str = "basic",
    context: dict[str, Any] | None = None,
) -> list[str]:
```

Returns at most 3 targeted calibration warnings.

| # | Given | When | Then |
|---|-------|------|------|
| 1 | score is outside `[0.0, 10.0]` | called | returns single warning: `f"Score {score} is outside the 0-10 range; check for parsing error."` and returns immediately |
| 2 | `context["mode"] == "debate"` and `score >= 9.5` | called | warns: reserves 9.5+ for proposals citing exact `file::function` paths covering all edge cases |
| 3 | `context["mode"] == "implement"` and `score <= 3.0` | called | warns: confirm code is truly broken or tests failing, not just incomplete |
| 4 | `score == 10.0` | called | warns: `"Score 10 claimed — confirm every criterion is fully satisfied with no improvements possible."` |
| 5 | `score == 0.0` | called | warns: `"Score 0 claimed — confirm the response is entirely absent or meaningless, not just poor."` |

---

## `_score_is_in_code_block` Function

```python
def _score_is_in_code_block(text: str, score_line_start: int) -> bool
```

Determines whether the character position `score_line_start` falls inside a markdown code block by scanning the text from the beginning and tracking two states:

- `in_fenced`: toggled by runs of 3 or more consecutive backticks (when not in inline code)
- `in_inline`: toggled by single backticks (when not in a fenced block)
- Runs of exactly 2 backticks have no effect

Returns `True` if either `in_fenced` or `in_inline` is active at `score_line_start`.

---

## `validate_calibration_anchors` Function

```python
def validate_calibration_anchors(
    text: str,
    evaluator_type: str = "basic",
    mode: str | None = None,
) -> list[str]:
```

Validates that extreme scores reference calibration anchor language from the evaluator system prompts.

### Trigger Thresholds

- **Low extreme**: `score <= 1.5`
- **High extreme**: `score >= 8.5`
- If score is not extreme, returns empty list immediately.
- If no `SCORE:` match is found via `r'SCORE:\s*([0-9]+(?:\.[0-9]+)?)'`, returns empty list.

### Anchor Keywords by Evaluator Type and Score Range

**Basic evaluator, low extreme** (`score <= 1.5`):
`"broken"`, `"dangerous"`, `"off-topic"`, `"fundamentally wrong"`, `"complete rewrite"`, `"trivial case"`, `"major requirement missed"`, `"partially correct"`, `"missing core functionality"`, `"fail basic tests"`, `"critical issue"`, `"severe flaw"`, `"unusable"`, `"incomplete"`

**Basic evaluator, high extreme** (`score >= 8.5`):
`"correct + specific"`, `"testable"`, `"tested"`, `"measurable"`, `"covers main requirement"`, `"pass code review"`, `"edge cases"`, `"named test"`, `"metric"`, `"every claim backed"`, `"comprehensive"`, `"thorough"`, `"well-structured"`, `"actionable"`, `"specific"`

**Diffusion evaluator, low extreme** (`score <= 1.5`):
`"catastrophic"`, `"irreversible"`, `"systemically destabilising"`, `"dangerous"`, `"breaks unrelated functionality"`, `"no mitigation"`, `"concerning"`, `"significant cascade"`, `"explicit mitigation"`, `"high risk"`, `"unacceptable risk"`, `"severe impact"`, `"cascading failure"`

**Diffusion evaluator, high extreme** (`score >= 8.5`):
`"minor"`, `"trivial ripple"`, `"easily addressed"`, `"negligible"`, `"zero maintenance"`, `"trivial rollback"`, `"bounded effects"`, `"clear mitigation"`, `"minimal impact"`, `"low risk"`, `"acceptable"`, `"contained"`, `"manageable"`, `"isolated"`

### Mode-Specific Anchor Extensions

When `mode` is provided, additional keywords are appended:

**Debate mode, low extreme**: `"vague"`, `"unspecific"`, `"no concrete plan"`, `"missing details"`, `"poor reasoning"`, `"unclear"`, `"incomplete analysis"`

**Debate mode, high extreme**: `"specific plan"`, `"concrete steps"`, `"clear reasoning"`, `"detailed analysis"`, `"well-justified"`, `"comprehensive plan"`

**Implement mode, low extreme**: `"syntax error"`, `"broken code"`, `"untested"`, `"import error"`, `"test failure"`, `"buggy"`, `"incorrect implementation"`

**Implement mode, high extreme**: `"working code"`, `"tests pass"`, `"clean implementation"`, `"well-tested"`, `"correct syntax"`, `"proper imports"`

### Analysis Section Extraction

The analysis section is extracted by finding `"ANALYSIS:"` in the text, then trimming at the first subsequent section header found among: `"TOP DEFECT:"`, `"KEY RISK:"`, `"DELTA VS PRIOR BEST:"`, `"ACTIONABLE FEEDBACK:"`, `"ACTIONABLE MITIGATIONS:"`, `"WHAT WOULD MAKE THIS 10/10:"`, `"SCORE:"`.

### Validation Checks

| # | Given | When | Then |
|---|-------|------|------|
| 1 | no anchor keyword (case-insensitive) found in the analysis section | checked | appends WARNING about extreme score without calibration anchor reference, citing two example anchor keywords |
| 2 | anchor keywords found | checked | logs debug with up to 3 matched anchors |
| 3 | extreme score and analysis section has fewer than 15 words | checked | appends WARNING about brief justification |
| 4 | extreme score and analysis lacks justification connectors (`"because"`, `"since"`, `"due to"`, `"as"`, `"given that"`) AND has fewer than 25 words | checked | appends WARNING about missing causal reasoning |
| 5 | low extreme score (`<= 1.5`) but analysis contains more than 2 of: `"good"`, `"excellent"`, `"great"`, `"well"`, `"properly"`, `"correctly"` | checked | appends issue about inconsistent positive language |
| 6 | high extreme score (`>= 8.5`) but analysis contains more than 2 of: `"bad"`, `"poor"`, `"wrong"`, `"incorrect"`, `"flawed"`, `"broken"`, `"missing"` | checked | appends issue about inconsistent negative language |

Note: checks 5 and 6 only run when `analysis_section` is non-empty AND anchor keywords were found (`found_anchor` is `True`). Their issue messages do NOT have a `"WARNING:"` prefix, making them hard errors.

---

## `validate_evaluator_output` Function

```python
def validate_evaluator_output(
    text: str,
    evaluator_type: str = "basic",
    mode: str | None = None,
) -> tuple[bool, list[str]]:
```

Validates the structural correctness of evaluator output. Returns `(is_valid, issues)` where `is_valid` is `True` only if ALL issues have a `"WARNING:"` prefix. Issues without the prefix are hard errors.

### Required Sections

**Basic evaluator**: `["ANALYSIS:", "TOP DEFECT:", "SCORE:"]`
- Defect section: `"TOP DEFECT:"`
- Feedback section: `"ACTIONABLE FEEDBACK:"`

**Diffusion evaluator**: `["ANALYSIS:", "KEY RISK:", "SCORE:"]`
- Defect section: `"KEY RISK:"`
- Feedback section: `"ACTIONABLE MITIGATIONS:"`

### Validation Rules

| # | Check | Severity | Detail |
|---|-------|----------|--------|
| 1 | `"SCORE:"` missing from text | **ERROR** | `"Missing required section: SCORE:"` |
| 2 | `"ANALYSIS:"` or defect section missing | WARNING | `"WARNING: Missing section: {section} — include for complete evaluation"` |
| 3 | `"DELTA VS PRIOR BEST:"` present but text after it is empty or shorter than 5 characters | WARNING | `"WARNING: 'DELTA VS PRIOR BEST:' has very short text — add descriptive comparison"` |
| 4 | SCORE line exists at line start but does not match `r'^SCORE:\s+\d+(?:\.\d+)?\b'` | **ERROR** | `"SCORE line malformed: '{score_line}' - expected 'SCORE: X.X' with optional trailing text"` |
| 5 | SCORE line is not the last line of output (after stripping) | WARNING | `"WARNING: SCORE should be the last line of the output for reliable parsing"` |
| 6 | `SCORE:` exists in text but not at line start (only when no line-start SCORE found) | WARNING | `"WARNING: SCORE: found but not at line start — place 'SCORE: X.X' on its own line"` |
| 7 | No SCORE line found at all (neither at line start nor anywhere) | **ERROR** | `"No SCORE line found"` |
| 8 | `SCORE:` found inside a markdown code block (via `_score_is_in_code_block`) | **ERROR** | `"SCORE: found inside markdown code block at line {line_num}"` (checks all lines, does not stop at first) |
| 9 | Analysis line starts with `A.`/`B.`/`C.`/`D.` but does not match `r'^[A-D]\.\s+.+:\s*\d+(?:\.\d+)?\s*—\s*.+$'` (em dash) | **ERROR** | Reports the line with expected format |
| 10 | Expected dimension names missing from ANALYSIS section | WARNING | Basic dimensions: `"A. Correctness"`, `"B. Completeness"`, `"C. Specificity"`, `"D. Architecture fit"`. Diffusion dimensions: `"A. Caller impact"`, `"B. Maintenance debt"`, `"C. Emergent behaviour"`, `"D. Rollback safety"` |
| 11 | Defect section value is not `"none"` but lacks `"::"` | **ERROR** | `"{defect_section} should reference file::function, got: '{defect_text}'"` |
| 12 | Defect section file part starts with `..`, `/`, `\`, or contains `..` | **ERROR** | Path traversal detected |
| 13 | `file::function` part (before `"--"` if present) does not match `r'^[\w\.\-]+::[\w\.\-]+$'` (after removing spaces) | **ERROR** | Invalid characters in file::function part |
| 14 | Feedback section present but contains no numbered items (`r'^\d+\.\s'`) | **ERROR** | `"{feedback_section} should contain numbered items (1., 2., etc.)"` |
| 15 | Feedback numbered items exist but none contain a file/function reference (neither `r'\b\w+\.py\s*[: ]+\s*\w+\b'` nor `"::"`) | WARNING | Lacks explicit file/function references |
| 16 | Critique/analysis sections found (via regex patterns) but none contain concrete file/function references | WARNING | Should contain concrete references for traceability |
| 17 | `"WHAT WOULD MAKE THIS 10/10:"` present, value is not `"already perfect"`, and text is shorter than 10 characters | **ERROR** | Should provide concrete improvement |
| 18 | Text length exceeds 8000 characters | WARNING | May exceed token budget |
| 19 | Score extracted successfully from SCORE line | -- | Runs `validate_score_calibration`; resulting warnings are prefixed with `"WARNING:"` |
| 20 | Calibration anchor validation | varies | Runs `validate_calibration_anchors(text, evaluator_type, mode)` and extends issues |

### Mode-Specific Validation

**Debate mode** (`mode == "debate"`):

| # | Check | Severity |
|---|-------|----------|
| 1 | Text does not contain `"text proposal"` (case-insensitive) AND does not contain `"planning round"` | **ERROR** |
| 2 | Text contains `"executed code"` or `"tool calls"` (case-insensitive) | WARNING |

**Implement mode** (`mode == "implement"`):

| # | Check | Severity |
|---|-------|----------|
| 1 | Text does not contain `"executed code"` (case-insensitive) AND does not contain `"code state"` | **ERROR** |
| 2 | Text does not contain `"file::"` AND does not contain `"function::"` | WARNING |

### Validity Determination

```python
is_valid = all(issue.startswith("WARNING:") for issue in issues)
```

An output is considered valid only when every issue in the list has a `"WARNING:"` prefix. Any issue without the prefix is a hard error that makes the output invalid.

---

## `DualEvaluator` Class

```python
class DualEvaluator:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm
```

### `evaluate` Method

```python
async def evaluate(
    self,
    subject: str,
    context: str,
    *,
    mode: Literal["debate", "implement", "reasoning"] = "debate",
    basic_system: str = "",
    diffusion_system: str = "",
    score_pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
) -> DualScore:
```

#### System Prompt Selection

| Mode | Basic system prompt | Diffusion system prompt |
|------|-------------------|----------------------|
| `"reasoning"` | `basic_system` or `default_prompts.REASONING_BASIC_SYSTEM` | `diffusion_system` or `default_prompts.REASONING_DIFFUSION_SYSTEM` |
| `"debate"` or `"implement"` | `basic_system` or `default_prompts.BASIC_SYSTEM` | `diffusion_system` or `default_prompts.DIFFUSION_SYSTEM` |

Custom system prompts (non-empty strings) override the defaults.

#### Mode Header Selection

```python
mode_header = _MODE_HEADERS.get(mode, _MODE_HEADERS["debate"])
```

If `mode` is not found in `_MODE_HEADERS`, falls back to the `"debate"` header.

#### User Message Construction

Both evaluators receive the same user message:

```
{mode_header}## Subject to Evaluate

{subject}

## Source Context

{context}
```

The message list is shallow-copied (`list(messages)`) for each evaluator call to prevent cross-contamination.

#### Parallel Execution

| # | Given | When | Then |
|---|-------|------|------|
| 1 | both LLM calls succeed | `evaluate` called | returns `DualScore` with both scores and formatted critiques |
| 2 | one or both LLM calls raise an exception | `evaluate` called | all undone tasks are cancelled via `t.cancel()` to prevent lingering background tasks consuming API quota; exception re-raised |

Both evaluators are launched as `asyncio.ensure_future` tasks and gathered with `asyncio.gather`. Neither evaluator sees the other's output (isolation guarantee).

#### Post-Processing Pipeline

1. Parse scores: `parse_score(resp.text, score_pattern)` for both responses
2. Validate output structure: `validate_evaluator_output(resp.text, evaluator_type)` for both; logs warnings on invalid output
3. Extract structured feedback: `extract_structured_feedback(resp.text, evaluator_type)` for both
4. Format critiques: `format_critique_from_feedback(feedback_dict)` for both
5. Compute combined score via temporary `DualScore` instance (verifies the formula works before final return)
6. Log info line with format: `"DualEvaluator[{mode}]: basic={score} (valid={bool}, feedback={count} items) diffusion={score} (valid={bool}, feedback={count} items) combined={score}"`
7. Log debug for basic defect and diffusion key risk if present
8. Return final `DualScore` with `ScoreItem(score, critique)` for both evaluators
