# Execution and System

This document specifies the agent's capabilities for executing commands, running tests, interacting with git, making network requests, and evaluating code -- along with the safety constraints that bound each capability.

## Context

The tools in this domain are the most powerful and the most dangerous. A shell command can install packages or delete the filesystem. A network request can exfiltrate data. A test runner can execute arbitrary code. These tools exist because the agent needs them to do its job (build, test, deploy), but every one operates under constraints that limit blast radius.

The guiding principle: **dedicated tools over generic shell**. Every operation that has a dedicated tool (reading files, searching, running tests, linting) should use that tool, not `bash`. The shell is a last resort for operations that have no dedicated tool -- builds, installs, custom scripts.

## Concern 1: Shell execution

### R-EXEC-01: Shell command execution with denylist

The agent must be able to execute shell commands in the workspace directory. A configurable denylist prevents execution of specific dangerous commands. The denylist is checked against the first token of every command segment (split on `&&`, `||`, `;`, `|`, `&`), so chained commands cannot bypass the check.

**Why:** The agent needs shell access for builds (`make`, `cargo build`), package management (`pip install`), and operations that have no dedicated tool. The denylist prevents the most dangerous commands (e.g., `rm -rf`, `chmod`, `shutdown`) from being executed even if the LLM hallucinates them.

**Acceptance criteria:**
- A command like `echo hi && rm -rf /` is rejected because `rm` appears in the denylist, even though `echo` does not.
- The denylist check strips path prefixes: `/usr/bin/rm` matches a denylist entry for `rm`.
- Quoted tokens are handled correctly: `"rm -rf /"` as a single quoted argument does not match `rm` as a leading token.
- A command not in the denylist executes normally and returns stdout, stderr, and exit code.

### R-EXEC-02: Command timeout

Every shell command must have a configurable timeout (default 60 seconds). When a command exceeds its timeout, the child process is killed and the agent receives a timeout error.

**Why:** A hanging build or an infinite loop in a script would stall the entire agent indefinitely. Timeouts ensure the agent always gets control back and can decide how to proceed.

**Acceptance criteria:**
- A command that runs for longer than the timeout is killed and returns a timeout error.
- The killed process is reaped (no zombie processes left behind).
- The timeout is per-invocation, not cumulative.

### R-EXEC-03: Shell is last resort

The shell tool's description and schema must actively discourage use for operations that have dedicated tools. Reading files via `cat`, searching via `grep`, running tests via `pytest`, and linting via `ruff` should all use their dedicated tools.

**Why:** Dedicated tools provide better output formatting (structured results vs. raw stdout), better error handling (parsed errors vs. stderr blobs), security validation (path checks), and resource caps (output truncation). Using `bash` for these tasks bypasses all of these benefits.

**Acceptance criteria:**
- The tool description explicitly lists which dedicated tools should be used instead of bash for common operations.
- This is a documentation/guidance requirement, not a runtime enforcement -- the agent can still use bash for anything not in the denylist.

## Concern 2: Python code evaluation

### R-EXEC-04: Python snippet execution in isolated subprocess

The agent must be able to execute Python snippets in a subprocess (never `exec()` in the harness process itself). The subprocess must have the workspace on `sys.path` so workspace modules are importable. The output must separate stdout, stderr, and the return value of the last expression.

**Why:** The agent sometimes needs to verify behavior, test an import, compute a value, or inspect runtime data that static analysis cannot reveal. Subprocess isolation ensures that a buggy snippet cannot corrupt the harness process's state, block the event loop, or leak memory.

**Acceptance criteria:**
- The snippet runs in a fresh subprocess with `cwd=workspace` and workspace prepended to `PYTHONPATH`.
- stdout, stderr, and the return value are reported separately.
- Output is truncated to a configurable character limit (default 4,000) to prevent context flooding.
- A tight default timeout (30 seconds) prevents runaway scripts.
- The snippet has no stdin (connected to `/dev/null`).

### R-EXEC-05: Python eval is not sandboxed

The Python eval tool executes arbitrary Python code. It does not sandbox the interpreter, restrict imports, or prevent filesystem access. It is suitable for development/CI use but must not be used in untrusted-user contexts.

**Why:** Full sandbox isolation (seccomp, namespace isolation, etc.) would add significant complexity and platform-specific dependencies for marginal benefit in the intended use case (developer's own machine, CI pipeline). The tool documents this limitation explicitly rather than providing a false sense of security.

**Acceptance criteria:**
- The tool's documentation states that it is not sandboxed.
- The workspace must be set (basic guard), but no further access restrictions are enforced on the subprocess.

## Concern 3: Test execution

### R-EXEC-06: Structured test runner

The agent must be able to run pytest and receive structured results: total/passed/failed/error/skipped counts, per-test outcomes, and condensed failure tracebacks. This is preferred over running pytest via bash.

**Why:** Raw pytest output is verbose and noisy -- the agent must parse it mentally to determine pass/fail status. Structured output gives the agent a summary it can act on immediately ("1 test failed, here is the traceback") without wasting context on passed-test noise.

**Acceptance criteria:**
- Returns summary counts: total, passed, failed, error, skipped.
- Returns per-test outcomes (test name + status).
- Returns condensed failure tracebacks (not full verbose tracebacks that consume excessive context).
- Supports extra pytest arguments (e.g., `-x` for fail-fast, `-k` for keyword selection).
- Respects a configurable timeout (default 120 seconds).
- Test path is validated against allowed paths.
- Supports both text and JSON output formats.

## Concern 4: Lint checking

### R-EXEC-07: Structured lint diagnostics

The agent must be able to run a linter (ruff) on specific files or directories and receive structured diagnostics: file, line, column, rule code, and message per diagnostic. Supports auto-fix mode for safely fixable issues and rule selection.

**Why:** Running `ruff` via bash returns raw text output that the agent must parse. Structured diagnostics let the agent identify the exact location and rule for each issue and decide whether to fix it manually or use auto-fix. This is especially useful after editing code to catch newly introduced issues before the commit hook.

**Acceptance criteria:**
- Each diagnostic includes file path, line number, column, rule code, and human-readable message.
- Auto-fix mode applies only fixes that ruff marks as safely fixable.
- Rule selection lets the agent check specific categories (e.g., unused imports only).
- File paths are validated against allowed paths.
- Output is capped to prevent a heavily-linted codebase from flooding the context.

## Concern 5: Git operations

### R-EXEC-08: Read-only git status, diff, and log

The agent must be able to read git working-tree status, view diffs (staged and unstaged), and view commit history. These are read-only operations with no side effects.

**Why:** The agent needs to understand the current state of the working tree to make informed decisions: what files have changed, what is staged for commit, what the recent commit history looks like. These are the git equivalent of the search tools.

**Acceptance criteria:**
- `git status` returns a short-format summary.
- `git diff` supports viewing staged changes separately from unstaged changes.
- `git log` returns recent commit history.
- All git commands run with a timeout (30 seconds) and in the workspace directory.
- A failed git command (e.g., not a git repository) returns an error result, not an exception.

### R-EXEC-09: Git write operations via bash

Git operations that modify state (commit, push, branch, checkout, merge) are performed via the bash tool, not via dedicated git write tools. This is a deliberate design decision.

**Why:** Git write operations are orchestrated by the agent loop (which manages commit messages, hook gating, and push policy), not by individual tools. Providing dedicated write tools would create a bypass path around the framework's commit controls. The bash tool's denylist can selectively restrict dangerous git operations if needed.

**Acceptance criteria:**
- No dedicated tools exist for `git commit`, `git push`, `git checkout`, or `git merge`.
- The agent can perform these operations via bash when appropriate.
- The framework's commit orchestration (in the agent loop) is the canonical path for making commits.

## Concern 6: Network access

### R-EXEC-10: Web search

The agent must be able to search the web (via DuckDuckGo) and fetch individual web pages. This tool is optional -- not registered by default -- because it requires outbound network access.

**Why:** The agent may need to look up API documentation, error messages, or library usage patterns that are not present in the codebase. Web search provides this capability when enabled. It uses the DuckDuckGo HTML endpoint (no API key required) and pure stdlib for zero external dependencies.

**Acceptance criteria:**
- Search returns a ranked list of title + URL + snippet entries.
- Page fetch returns cleaned text (HTML stripped, boilerplate collapsed), not raw HTML.
- Long pages are truncated to a configurable character limit.
- Network errors return error results, not exceptions.
- The tool is not registered by default; it must be explicitly enabled via `extra_tools`.
- No JavaScript rendering -- only static HTML content is fetchable.

### R-EXEC-11: Generic HTTP client

The agent must be able to send HTTP requests (GET, POST, PUT, DELETE, PATCH, HEAD) with custom headers and body. This tool is optional -- not registered by default -- because it requires outbound network access.

**Why:** The agent may need to interact with APIs (check a service status, post a webhook, fetch structured data). This is a general-purpose escape hatch for network interactions beyond web search.

**Acceptance criteria:**
- Supports all standard HTTP methods.
- Automatically sets `Content-Type: application/json` when a dict body is provided.
- Response bodies are truncated to a configurable character limit.
- Network errors, HTTP errors, and timeouts return error results, not exceptions.
- The tool is not registered by default; explicit opt-in required.

## Concern 7: Operational safety constraints (cross-cutting)

### R-EXEC-12: All subprocess operations use async I/O

Every tool that runs a subprocess (bash, test runner, python eval, git, lint) must use async subprocess APIs or thread-pool executors. Blocking the asyncio event loop is prohibited.

**Why:** The harness is async throughout. A blocking subprocess call would freeze all concurrent operations (including timeout enforcement). Async subprocess ensures the event loop remains responsive.

**Acceptance criteria:**
- All subprocess-spawning tools use `asyncio.create_subprocess_exec` or `asyncio.create_subprocess_shell`.
- File I/O operations use `asyncio.to_thread` to avoid blocking.
- No tool's `execute` method contains synchronous `subprocess.run` or `os.system` calls.

### R-EXEC-13: Process cleanup on timeout

When a subprocess is killed due to timeout, the child process must be reaped (waited on) to prevent zombie processes and asyncio transport warnings.

**Why:** On POSIX systems, a killed child process remains as a zombie until its parent waits on it. Accumulating zombies wastes PID table entries and triggers asyncio warnings about unclosed transports.

**Acceptance criteria:**
- After killing a timed-out process, `proc.wait()` is called.
- No zombie processes remain after a timeout.
- The timeout error message includes the timeout duration.

### R-EXEC-14: Output truncation on all execution tools

Every tool that can produce unbounded output must enforce a character or line cap. The cap must be configurable where appropriate but must have a default that prevents context flooding.

**Why:** A `grep` that matches 10,000 lines, a test suite with 500 test results, or a subprocess that prints a build log -- any of these can consume the agent's entire context window if uncapped. Output truncation is a safety net for context budget management.

**Acceptance criteria:**
- Bash tool output includes stdout + stderr + exit code, capped by the subprocess output size.
- Test runner output includes a summary even when the full test list is truncated.
- Python eval output is capped at its configured character limit (default 4,000).
- Lint output is capped at its configured character limit.
- Truncated output includes a note indicating truncation occurred.
