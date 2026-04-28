# Verification Hooks

User stories for the pluggable post-execution verification hooks that gate or advise commit decisions. All hooks run after the agent's execution phase completes and before changes are committed.

---

# Hook Framework

## US-01: As a cycle, I need verification hooks to produce a structured pass/fail result with output and error details so that downstream logic can make commit decisions and surface diagnostics consistently

Every hook must return a uniform result structure containing a pass/fail verdict, a human-readable output (on success), and error details (on failure). This consistency lets the runner process all hooks identically without hook-specific handling.

### Acceptance Criteria
- Given a hook that finds no issues, when it completes, then it returns a passing result with a descriptive success message
- Given a hook that finds issues, when it completes, then it returns a failing result with error details describing what went wrong

## US-02: As a cycle, I need certain hooks designated as commit-gating so that their failure prevents the commit and suppresses subsequent hooks, while advisory hooks run regardless

Some hooks catch errors severe enough that committing would be dangerous (syntax errors, broken imports). These must block the commit and skip remaining hooks. Other hooks (like test suites) are informational and should not prevent the commit even if they fail.

### Acceptance Criteria
- Given a commit-gating hook that fails, when the hook pipeline processes the result, then the commit is blocked and no subsequent hooks in the same phase are executed
- Given an advisory (non-gating) hook that fails, when the hook pipeline processes the result, then the failure is recorded but the commit proceeds and subsequent hooks still run

---

# Syntax Verification

## US-03: As a cycle, I need all source files matching configured patterns compiled to check for syntax errors so that a syntactically broken file is caught before it is committed

A syntax error in a committed file will break the next execution cycle entirely. Compiling every matching file is a cheap, zero-dependency check that catches the most catastrophic class of errors.

### Acceptance Criteria
- Given a workspace with all syntactically valid source files, when the syntax check runs, then it passes with a success message
- Given a workspace containing a file with a syntax error, when the syntax check runs, then it fails with an error message identifying the file and the nature of the syntax error
- Given custom file patterns configured for the syntax check, when the check runs, then only files matching those patterns are checked
- Given the default configuration, when the syntax check runs, then all files matching the default source pattern are checked

---

# Import Smoke Test

## US-04: As a cycle, I need critical modules imported in a fresh subprocess so that import errors and load-time failures are caught before the commit, not during the next execution cycle

A syntax check only catches parse-level errors. Import smoke testing catches a broader class of failures: missing dependencies, circular imports, undefined names at module scope, and broken class definitions that only surface when the module is actually loaded. Running in a subprocess ensures the test is not masked by already-cached modules in the current process.

### Acceptance Criteria
- Given all configured modules importable without error, when the import smoke test runs, then it passes with a message indicating how many modules were checked
- Given a module that raises an error on import, when the import smoke test runs, then it fails with the error output (truncated to a reasonable length)
- Given the subprocess hangs beyond the configured timeout, when the timeout fires, then the subprocess is killed and the hook reports a timeout failure
- Given the Python interpreter is not found at the expected path, when the hook attempts to launch the subprocess, then it fails with a clear error rather than an unhandled exception

## US-05: As a cycle, I need configurable smoke-call statements executed after imports so that runtime-only errors hidden inside function bodies are caught before commit

Some errors only manifest when a specific function is called, not when the module is imported. Smoke calls are arbitrary statements that exercise critical code paths (e.g., calling a validator function) to catch these runtime-only failures before they reach production.

### Acceptance Criteria
- Given smoke-call statements configured for the hook, when the import smoke test runs, then each statement is executed after the imports complete
- Given a smoke-call statement that raises an error, when the hook processes the subprocess output, then the hook fails with the error details

## US-06: As a cycle, I need the import smoke test to exercise tool class loading when the workspace is the harness itself so that a refactoring that breaks tool registration is caught immediately

When the harness is improving its own source code, a broken tool class or missing registration will prevent the next cycle from starting. Exercising the tool registry build as part of the import smoke test catches this specific failure mode.

### Acceptance Criteria
- Given the workspace is the harness's own source directory, when the import smoke test runs, then it includes a step that builds the tool registry
- Given the workspace is an external project, when the import smoke test runs, then the tool registry build step is skipped

---

# Static Analysis

## US-07: As a cycle, I need changed files checked for undefined names, redefined names, and unused imports so that semantic errors that pass syntax checking are caught before commit

Syntax checking misses errors like calling a function that was never defined, accidentally redefining a variable, or importing a module that is never used (often a sign of a merge artifact). Static analysis catches these higher-level errors that will cause runtime failures or indicate code quality problems.

### Acceptance Criteria
- Given changed files with no static analysis findings, when the check runs, then it passes with a message indicating how many files were checked
- Given a changed file containing a reference to an undefined name, when the check runs, then it fails with the specific finding
- Given no changed files in the current phase, when the check runs, then it passes immediately (the check is scoped to changed files, not the whole tree)

## US-08: As a cycle, I need the static analysis hook to gracefully degrade when no analysis tool is installed so that the build is not blocked on a fresh environment without development dependencies

On a fresh installation or a minimal deployment, the analysis tool may not be installed. The hook should log a warning and pass (with an advisory message) rather than failing the build, so that the absence of a development dependency does not prevent the harness from functioning.

### Acceptance Criteria
- Given neither the primary nor fallback analysis tool is installed, when the hook runs, then it passes with a warning message recommending installation, and the advisory message is recorded in the error field
- Given the primary analysis tool is installed, when the hook runs, then it uses the primary tool
- Given only the fallback tool is installed, when the hook runs, then it uses the fallback tool

---

# Test Execution

## US-09: As a cycle, I need the project's test suite executed after changes so that regressions are detected before the change is considered complete

Running the test suite is the strongest automated signal of whether a change broke existing behavior. Test results feed back into the evaluation and planning process so the agent can fix regressions in subsequent iterations.

### Acceptance Criteria
- Given all tests pass, when the test hook runs, then it returns a passing result with the full test output
- Given one or more tests fail, when the test hook runs, then it returns a failing result with the test output (including failure details and tracebacks)
- Given the test runner is not installed, when the hook runs, then it fails with a clear "not found" error
- Given the test suite exceeds the configured timeout, when the timeout fires, then the subprocess is killed and the hook reports a timeout failure

---

# Version Control Commit

## US-10: As a cycle, I need changes committed to configured repositories with a structured commit message so that every execution phase produces a traceable commit

After verification hooks pass, changes should be committed with a message that identifies the execution round, phase, and (optionally) evaluation scores. This creates a reviewable history of the agent's work that can be audited, compared, or reverted.

### Acceptance Criteria
- Given one or more configured repositories with changes, when the commit hook runs, then each repository receives a commit with a message identifying the round and phase
- Given a repository directory that does not exist, when the commit hook runs, then that repository is skipped with a note and the remaining repositories are still processed
- Given a git command that times out, when the timeout fires, then the failure is recorded and the remaining repositories are still processed

## US-11: As a cycle, I need commit messages optionally enriched with evaluation metadata so that the commit history captures not just what changed but how well it scored and why

Rich commit metadata (scores, file lists, critique summaries, inner-round statistics) turns the git log into a searchable record of the agent's quality trajectory. This is essential for post-run analysis and for understanding which iterations actually improved the code.

### Acceptance Criteria
- Given rich metadata mode is enabled, when the commit message is built, then it includes the evaluation score in the subject line and a body containing the changes summary, modified files list, inner-round scores, tool usage, and evaluator critiques
- Given rich metadata mode is disabled, when the commit message is built, then it contains only the round and phase identifier
- Given a modified files list longer than the display limit, when the commit body is built, then only the first entries are shown with a count of how many more were omitted
