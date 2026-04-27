# Verification Hooks Requirements

This document defines the requirements for verification hooks: the post-execution quality gates that validate the agent's changes before they are committed.

Verification hooks answer one question: **did the agent break anything, and should its changes be accepted?**

The hooks form a pipeline that runs after each execution phase. They are the last line of defense between an LLM-generated code change and a permanent commit. Without them, the agent can silently introduce syntax errors, undefined references, broken imports, and failing tests that propagate to subsequent rounds and compound into unrecoverable states.

---

## 1. Hook Pipeline Architecture

### R-HOOK-01: Ordered hook execution with gating

Hooks must execute in a defined order. Certain hooks are designated as "gates" -- if a gating hook fails, all subsequent hooks in the pipeline are skipped and the phase's changes are not committed.

**Why:** Running a test suite after a syntax error wastes time and produces misleading output (hundreds of import failures that mask the real problem). Gating ensures that cheap, fast checks (syntax, imports) run first and block expensive checks (tests) when the basics are broken.

**Acceptance criteria:**

- Given a syntax error in a modified file, when the hook pipeline runs, then the syntax check fails, subsequent hooks (import smoke, static analysis, tests) are skipped, and the commit does not occur.
- Given all gating hooks passing, when the pipeline continues, then non-gating hooks (e.g., test suite) run and their failure is reported but does not block the commit.

### R-HOOK-02: Structured hook results

Every hook must return a structured result containing: a pass/fail status, output text (for display/logging), and an error description (for diagnostics). Hooks must never raise unhandled exceptions -- all failures must be captured in the result structure.

**Why:** An unhandled exception in a hook would crash the entire agent run. A missing error description forces operators to grep through raw logs. Structured results enable automated monitoring, dashboards, and trend analysis across runs.

**Acceptance criteria:**

- Given a hook that encounters an internal error (e.g., the linter binary crashes), when it completes, then it returns a failing result with a descriptive error, not an unhandled exception.
- Given a hook that passes, when the result is inspected, then it contains both the pass status and descriptive output (e.g., "All syntax checks passed").

---

## 2. Syntax Checking

### R-HOOK-03: Compile-time syntax validation of all modified Python files

After the agent modifies files, all Python files matching configured glob patterns must be compiled to bytecode to verify syntactic correctness.

**Why:** The LLM frequently produces syntactically invalid Python (unmatched parentheses, invalid indentation, stray characters from conversation context). A syntax error that reaches the repository will break every subsequent import and tool invocation, cascading into a complete agent failure in the next round.

**Acceptance criteria:**

- Given a Python file with a missing closing parenthesis, when syntax checking runs, then the hook fails with an error message naming the file and describing the syntax error.
- Given a workspace with 50 valid Python files, when syntax checking runs, then it passes and reports success.
- Given custom glob patterns (e.g., `["src/**/*.py"]`), when syntax checking runs, then only files matching those patterns are checked.

### R-HOOK-03a: Syntax check is a gating hook

Syntax checking must be designated as a gating hook. Its failure must prevent all downstream hooks and the commit.

**Why:** There is no point running import checks, static analysis, or tests when the code cannot even parse. Every downstream check will produce noise errors that obscure the root cause.

---

## 3. Import Validation

### R-HOOK-04: Subprocess-based import smoke test

After the agent modifies code, the configured modules must be importable in a fresh Python subprocess. The test must run in a separate process, not in the current process.

**Why:** The harness's own process has already imported all modules. A broken import would be masked by Python's module cache (`sys.modules`). Only a fresh subprocess reveals the actual import-time behavior. This is especially critical in self-improvement mode, where the agent edits the harness's own source -- a broken import would prevent the next round from starting at all.

**Acceptance criteria:**

- Given a module that was importable before the agent's changes and is still importable after, when the import smoke test runs, then it passes.
- Given a module where the agent deleted a function that is referenced at import time, when the import smoke test runs in a fresh subprocess, then it fails with the import error.
- Given the import smoke test, when it runs, then it uses the same Python interpreter and virtual environment as the harness itself (not a system `python` that may have different packages).

### R-HOOK-04a: Import smoke test is a gating hook

Import validation must be designated as a gating hook. If imports are broken, downstream tests and commits are meaningless.

### R-HOOK-05: Runtime smoke calls for deferred errors

Beyond top-level imports, the smoke test must support executing configurable runtime expressions that exercise code paths where errors hide inside function bodies (NameError, AttributeError).

**Why:** Python's lazy evaluation means a `NameError` inside a function body is not caught at import time. The 2026-04-19 `validate_calibration_anchors` incident demonstrated this: `import` succeeded, but the first evaluator call crashed because a referenced helper was never defined. Runtime smoke calls catch these deferred errors.

**Acceptance criteria:**

- Given a smoke call that invokes a function referencing an undefined name, when the import smoke test runs, then it fails with the NameError (not at import time, but at call time).
- Given no configured smoke calls, when the import smoke test runs, then it only checks imports (backward compatible).

### R-HOOK-06: Import smoke timeout protection

The import smoke subprocess must be bounded by a configurable timeout. A hung import (e.g., a module that makes a network call at import time) must not block the hook pipeline indefinitely.

**Why:** Some modules perform I/O at import time (connecting to databases, downloading models). If the network is down, the import hangs forever. The timeout ensures the hook pipeline continues.

**Acceptance criteria:**

- Given an import that hangs for longer than the timeout, when the timeout expires, then the subprocess is killed and the hook returns a failing result with "timed out."
- Given a normal import that completes within the timeout, when the import smoke test runs, then the timeout has no effect.

---

## 4. Static Analysis

### R-HOOK-07: Undefined name detection (F821)

After the agent modifies files, all changed Python files must be checked for references to undefined names.

**Why:** The LLM frequently writes calls to functions it never defined, references variables from a different scope, or hallucinates helper functions. These produce NameError at runtime but are invisible at import time (they are inside function bodies). Static analysis catches them before commit.

**Acceptance criteria:**

- Given a Python file containing a call to `validate_results()` where no such function is defined or imported, when static analysis runs, then the hook fails with an error identifying the undefined name and the file.
- Given a Python file where all referenced names are properly defined or imported, when static analysis runs, then the hook passes.

### R-HOOK-08: Scoped checking -- only changed files

Static analysis must run only on files that were changed in the current phase, not the entire codebase.

**Why:** Running a full-codebase lint on every commit is slow and produces pre-existing warnings that drown out new issues. The agent is only responsible for errors it introduced.

**Acceptance criteria:**

- Given 3 changed files and 100 unchanged files with pre-existing warnings, when static analysis runs, then only the 3 changed files are checked.
- Given no changed Python files in the current phase, when static analysis runs, then it passes immediately with no work done.

### R-HOOK-09: Graceful degradation when no linter is installed

If no supported linting tool (ruff, pyflakes) is available in the current environment, static analysis must pass with a warning, not fail.

**Why:** Blocking the build because a development tool is missing creates a chicken-and-egg problem on fresh installs and prevents local development on minimal environments. The hook should encourage installing the tool but not require it.

**Acceptance criteria:**

- Given an environment where neither ruff nor pyflakes is installed, when static analysis runs, then it passes but the output includes a warning naming the missing tools and how to install them.
- Given an environment where ruff is installed, when static analysis runs, then ruff is preferred over pyflakes.

### R-HOOK-09a: Static analysis is a gating hook

Static analysis must be designated as a gating hook when a linter is available. Undefined names detected by static analysis prevent the commit.

---

## 5. Test Execution

### R-HOOK-10: Automated test suite execution

The hook pipeline must support running the project's test suite (via pytest) against a configurable test directory.

**Why:** Syntax correctness and import validity do not guarantee behavioral correctness. Tests verify that the agent's changes produce the expected behavior. Running tests after every phase catches regressions before they accumulate.

**Acceptance criteria:**

- Given a test suite where all tests pass, when the pytest hook runs, then it returns a passing result with the test output.
- Given a test suite where 2 tests fail, when the pytest hook runs, then it returns a failing result with the failure details.
- Given a configurable test path, when it is set to `tests/unit/`, then only tests in that directory are executed.

### R-HOOK-10a: Test failure does not gate the commit by default

Test execution must NOT be a gating hook by default. Test failures are reported but do not block the commit.

**Why:** In some phases (e.g., "add new tests"), the agent is expected to create tests that initially fail. Gating the commit on test passage would prevent the agent from committing the test file, which is the intended output of the phase.

### R-HOOK-11: Test execution timeout

The test subprocess must be bounded by a configurable timeout. Tests that hang (infinite loops, deadlocks) must not block the hook pipeline.

**Acceptance criteria:**

- Given a test that enters an infinite loop, when the timeout expires, then the subprocess is killed, resources are cleaned up, and the hook returns a failing result with "timed out."

---

## 6. Commit Automation

### R-HOOK-12: Conditional commit after successful gating hooks

After all gating hooks pass, the hook pipeline must support automatically committing changes to configured repositories with a structured commit message.

**Why:** Manual commits between phases break the automation loop. Automatic commits create a recoverable history: if a later phase breaks the code, the operator can revert to the last good commit without re-running previous phases.

**Acceptance criteria:**

- Given all gating hooks passed, when the commit hook runs, then changes in configured repositories are staged and committed.
- Given a repository path that does not exist, when the commit hook runs, then that repository is skipped with a diagnostic message (not a crash).

### R-HOOK-13: Rich commit metadata

Commit messages must optionally include structured metadata: round number, phase name, evaluation score, files changed, critique summaries, and tool usage statistics.

**Why:** When reviewing git history, operators need to quickly assess what each automated commit accomplished and how well it scored. A bare `[harness] R3 phase_2` tells nothing about quality; a commit with score, file list, and evaluator critiques lets the operator triage without opening artifacts.

**Acceptance criteria:**

- Given rich metadata mode enabled, when a commit is created, then the commit message includes the round number, phase name, score, list of modified files (up to 10, with overflow count), inner-round scores, and evaluator critique summaries (truncated to prevent excessively long messages).
- Given rich metadata mode disabled, when a commit is created, then the commit message is a concise one-liner with round and phase identification only.

### R-HOOK-14: Commit timeout and error isolation

Each git operation (add, commit) must be individually bounded by a timeout. A failure in one repository must not prevent commits to other configured repositories.

**Why:** Git operations on large repositories or network-mounted filesystems can hang. A single hung commit must not block the entire pipeline or prevent commits to other, healthy repositories.

**Acceptance criteria:**

- Given two configured repositories where one hangs during `git add`, when the timeout expires, then the hung repository is reported as failed and the other repository is committed normally.
- Given a git commit that fails (e.g., nothing to commit), when the error occurs, then it is captured in the result and the hook continues to the next repository.
