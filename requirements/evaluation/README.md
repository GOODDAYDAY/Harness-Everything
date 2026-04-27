# Evaluation Domain

The evaluation domain answers one question: **"Was this cycle's output actually good?"**

An autonomous coding agent runs in cycles -- debate (propose), implement (execute), reasoning (explore without changing code). After each cycle, the system must judge whether the output advanced the project. This judgment drives the next cycle: a high score means "keep going in this direction," a low score means "try something different."

Evaluation has two layers that serve fundamentally different purposes:

1. **Static analysis** provides objective ground truth -- syntax errors, broken imports, disappeared symbols. These are facts, not opinions.
2. **LLM-based dual evaluation** provides subjective quality judgment -- is the reasoning sound? are edge cases covered? does the change create systemic risk? These require intelligence, and intelligence requires calibration.

The two layers feed into each other: static analysis findings are injected into the LLM evaluator's prompt so that subjective judgment is anchored by objective evidence. An evaluator cannot give a high score to code that objectively fails to compile.

---

## Static Analysis

### Why deterministic checks exist alongside LLM evaluation

LLMs hallucinate. An LLM evaluator can read broken code and declare it correct. Static analysis provides an un-gameable floor: if `py_compile` says the syntax is wrong, the syntax is wrong, regardless of what the LLM thinks.

### Scenario: Syntax validation

A cycle writes three Python files. One of them has a missing colon on line 47.

**Expected behavior:**
- The system runs `py_compile` on all three files
- The broken file produces an ERROR finding with the file path and line number
- The other two files pass silently
- The report summary reads "1 error(s), 0 warning(s), 2 file(s) clean"

**Acceptance criteria:**
- Every `.py` file in the changed set is compiled; non-Python files are counted as skipped, not silently ignored
- A syntax error always produces an ERROR-level finding, never a warning
- The finding includes the file path and line number extracted from the compiler output
- Files that do not exist on disk are skipped with a count, not an exception

### Scenario: Import and symbol verification

A cycle adds `from harness.core.llm import LLMCLient` (typo -- the real class is `LLM`).

**Expected behavior:**
- The system parses all imports via AST (no actual importing -- no side effects)
- It resolves `harness.core.llm` to a file in the workspace and parses that file's top-level names
- `LLMCLient` is not among them; the system produces an ERROR finding
- For modules outside the workspace (stdlib, pip packages), the system checks only whether the top-level module is findable, not whether every symbol exists (too expensive and fragile)

**Acceptance criteria:**
- An import of a module that exists nowhere (not stdlib, not installed, not in workspace) produces a WARN finding
- An import of a specific symbol from a workspace module that does not export that symbol produces an ERROR finding
- The system never imports a module to check it -- all checks are AST-based or use `importlib.util.find_spec`
- Re-exports via `__init__.py` are recognized (a package that does `from .submodule import Foo` exports `Foo`)

### Scenario: Structural regression detection

A cycle edits `harness/tools/bash.py` and accidentally deletes the `BashTool` class definition.

**Expected behavior:**
- The system compares top-level class/function names before and after
- `BashTool` existed before but is absent after; the system produces a WARN finding
- New names that appear are not flagged (additions are not regressions)

**Acceptance criteria:**
- Structural comparison only runs when a "before" snapshot is available; new files are skipped
- Only top-level names are compared (nested functions/classes are not tracked)
- Removed names produce WARN, not ERROR -- the removal may be intentional

### Report formatting for LLM consumption

The static report is formatted as a markdown block with a summary line and a table of findings. This format exists because the LLM evaluator reads it as part of its prompt context.

**Acceptance criteria:**
- When no Python files were changed, the report is empty (no markdown block at all)
- When all files pass, the report says so explicitly with a one-line confirmation
- ERROR findings trigger a bold warning that the evaluator "MUST FAIL this execution"
- Pipe characters in file paths and messages are escaped to avoid breaking the markdown table

---

## Metrics

### Why scoring discrimination matters

The hardest scores to assign are in the 4-7 range. A score of 2 (obviously broken) and a score of 9 (obviously excellent) are easy. But the difference between a 5.0 (specific but incomplete) and a 6.0 (working code with gaps) is where evaluator quality shows.

If an evaluator gives everything in the middle range the same score, it provides no signal for the system to improve. Discrimination in the critical range is the single most important metric for evaluator health.

### Scenario: Measuring evaluator discrimination

Over 20 evaluation cycles, the system collects all scores that fell in the 4.0-7.0 range.

**Expected behavior:**
- The system computes the sample standard deviation (N-1 denominator) of these scores
- A healthy evaluator produces a standard deviation of at least 0.5 (scores are spread out)
- An evaluator that gives everything 5.5 produces a standard deviation near 0 (alarm signal)

**Acceptance criteria:**
- Uses sample standard deviation (N-1), not population standard deviation (N), because the sample sizes are small (typically 5-15 scores in the critical range)
- Returns 0.0 when fewer than 2 scores fall in the critical range (cannot compute deviation from a single point)
- Invalid or non-numeric score entries are silently skipped, not exceptions
- The critical range is 4.0-7.0 inclusive on both ends
