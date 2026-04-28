# Execution and System

User stories covering capabilities for running shell commands, evaluating Python snippets, running tests, querying git state, and searching the web.

**Actors:**
- "As the agent" -- the agent executing commands and queries to accomplish tasks
- "As a file operation" -- security constraints on execution (reused for consistency with the domain's security actor)

---

## Shell Execution

## US-01: As the agent, I need to execute shell commands in the workspace directory so that I can run builds, install packages, and perform system operations that have no dedicated tool

Shell execution is a last-resort capability for operations like package installation, build commands, custom scripts, and git push -- tasks that no specialized tool handles. The command runs in the workspace directory with captured output.

### Acceptance Criteria
- Given a shell command, when the agent executes it, then the command runs in the workspace directory and stdout, stderr, and exit code are returned
- Given a command that exits with a non-zero code, when the result is returned, then it is marked as an error and both stdout and stderr are included
- Given a command that runs longer than the timeout, when the timeout expires, then the command is killed, the child process is cleaned up, and a timeout error is returned

## US-02: As a file operation, I need shell commands checked against a denylist so that dangerous commands cannot be executed by the agent

A configurable denylist of command names prevents the agent from running destructive or unauthorized commands. The check inspects every segment of a chained command (separated by shell operators like && or ||) so that hiding a denied command after a benign one is caught.

### Acceptance Criteria
- Given a denied command as the first token, when the agent tries to execute it, then a permission error is returned naming the denied command
- Given a denied command chained after a benign command (e.g., via && or ||), when the agent tries to execute it, then the entire command is rejected
- Given a denied command invoked with a full path (e.g., /usr/bin/rm), when the agent tries to execute it, then the base name is matched against the denylist and the command is rejected
- Given an empty or absent denylist, when any command is executed, then no denylist check is applied

---

## Python Evaluation

## US-03: As the agent, I need to execute a Python code snippet in an isolated subprocess so that I can verify imports, check return values, and run quick assertions without full test overhead

The agent provides a Python snippet that runs in a fresh subprocess with the workspace automatically on the import path. The tool captures stdout, stderr, and the return value of the last expression separately, giving structured output.

### Acceptance Criteria
- Given a Python snippet, when the agent evaluates it, then stdout, stderr, and the exit code are returned as separate sections
- Given a snippet whose last statement is an expression (not an assignment), when the agent evaluates it, then the expression's representation is returned as a distinct return value
- Given a snippet that imports a workspace module, when the agent evaluates it, then the import succeeds because the workspace is on the import path

## US-04: As the agent, I need Python evaluation to enforce a tight timeout so that infinite loops or expensive operations are caught quickly

The default timeout for snippet evaluation is shorter than for general shell commands, and it is clamped to a maximum to prevent the agent from setting an unreasonably long timeout.

### Acceptance Criteria
- Given a snippet that runs an infinite loop, when the timeout expires, then the subprocess is killed and a timeout error is returned with guidance about common causes
- Given a timeout request exceeding the maximum, when the snippet is evaluated, then the timeout is clamped to the maximum

## US-05: As the agent, I need Python evaluation output to be truncated to a configured limit so that verbose scripts do not flood my context window

Output from the snippet is capped at a configurable maximum character count. When truncation occurs, the exit code trailer is preserved so the agent always knows whether the snippet succeeded.

### Acceptance Criteria
- Given output exceeding the character limit, when the result is returned, then it is truncated with a notice and the exit code is still visible
- Given output within the character limit, when the result is returned, then it is shown in full

---

## Test Running

## US-06: As the agent, I need to run tests and receive structured results so that I can programmatically determine what passed, failed, and why

The agent provides a test path and optional arguments. The tool invokes the test framework, parses the output, and returns structured data: total/passed/failed/error/skipped counts, per-test outcomes, and condensed failure tracebacks sized to fit a context window.

### Acceptance Criteria
- Given a test directory, when the agent runs tests, then the result includes counts (passed, failed, error, skipped, total) and per-test outcomes with test identifiers
- Given test failures, when the result is returned, then condensed failure tracebacks are included showing the assertion or error for each failure
- Given all tests passing, when the result is returned, then the result is not marked as an error (test failures are informational, not tool errors)

## US-07: As the agent, I need test results in either human-readable or structured data format so that I can choose based on downstream needs

The test runner supports both a compact text format (designed for LLM context) and a JSON format (for programmatic processing).

### Acceptance Criteria
- Given a text format request, when tests complete, then the output includes a summary line, per-test outcome symbols, and failure sections in a compact format
- Given a JSON format request, when tests complete, then the output is valid JSON with counts, per-test results, and failure details

## US-08: As the agent, I need the test runner to handle tool-level vs. test-level failures differently so that I know when the tool itself broke vs. when tests simply failed

Test failures (exit code indicating failed assertions) are reported as informational results, not tool errors. Only infrastructure failures (interrupted, internal errors, usage errors) are reported as tool errors.

### Acceptance Criteria
- Given tests that fail due to assertion errors, when the result is returned, then it is marked as a non-error result (the agent should read the failures, not treat the tool as broken)
- Given an infrastructure failure (e.g., test collection error), when the result is returned, then it is marked as a tool error

## US-09: As the agent, I need the test runner to respect a timeout so that stuck tests do not block the agent indefinitely

A configurable timeout kills the test process if it runs too long. The child process is properly cleaned up to avoid zombie processes.

### Acceptance Criteria
- Given a test suite that hangs, when the timeout expires, then the test process is killed and a timeout error is returned
- Given a test suite that completes within the timeout, when the result is returned, then it includes the execution duration

---

## Git Queries

## US-10: As the agent, I need to check the working tree status so that I can see which files are modified, staged, or untracked

The agent can query the current state of the workspace's git working tree, receiving a summary of file statuses.

### Acceptance Criteria
- Given a git repository, when the agent queries status, then all modified, staged, added, deleted, and untracked files are listed with their status codes
- Given a non-git workspace, when the agent queries status, then a git error is returned

## US-11: As the agent, I need to see file-level diffs from git so that I can understand exactly what has changed since the last commit

The agent can view unstaged changes (default) or staged changes, optionally limited to a specific file or directory.

### Acceptance Criteria
- Given unstaged changes, when the agent queries diff, then the unified diff of all unstaged modifications is returned
- Given the staged flag, when the agent queries diff, then only staged (cached) changes are shown
- Given a specific file path, when the agent queries diff, then only changes to that file are shown

## US-12: As the agent, I need to view the recent commit log so that I can understand the history and evolution of the codebase

The agent specifies how many commits to view and whether to use a compact one-line format.

### Acceptance Criteria
- Given a commit count, when the agent queries the log, then that many recent commits are shown
- Given the one-line format flag, when the agent queries the log, then each commit is shown as a single line with hash and message

## US-13: As the agent, I need git commands to respect a timeout so that slow or hanging git operations do not block me indefinitely

Git operations enforce a timeout. If a git command takes too long (e.g., on a very large repository), it is killed and an error is returned.

### Acceptance Criteria
- Given a git command that exceeds the timeout, when the timeout expires, then the git process is killed and a timeout error is returned

---

## Web Search

## US-14: As the agent, I need to search the web so that I can find documentation, error explanations, and solutions to problems I encounter

The agent provides a search query and receives a ranked list of results with titles, URLs, and text snippets. No API key is required.

### Acceptance Criteria
- Given a search query, when the agent searches, then results with titles, URLs, and snippets are returned
- Given a query with no results, when the search completes, then a message indicates no results were found with a suggestion to rephrase
- Given a network error, when the search fails, then a descriptive error is returned without raising an exception

## US-15: As the agent, I need to fetch and read the text content of a web page so that I can read documentation or reference material

The agent provides a URL and receives the page's content converted to clean plain text (HTML tags stripped, boilerplate removed). Long pages are truncated to fit within a context budget.

### Acceptance Criteria
- Given a valid URL, when the agent fetches it, then the page content is returned as clean plain text with HTML tags and boilerplate removed
- Given a page exceeding the character limit, when the content is returned, then it is truncated with a head-and-tail excerpt preserving both the beginning and end of the page
- Given an invalid URL (not starting with http/https), when the agent tries to fetch it, then a validation error is returned
- Given a network or HTTP error, when the fetch fails, then a descriptive error is returned

## US-16: As the agent, I need web search to be an opt-in capability so that offline or air-gapped environments are not affected by its availability

Web search requires network access and is not registered by default. It must be explicitly enabled via configuration so that environments without network access are not burdened with an unusable tool in their schema.

### Acceptance Criteria
- Given a default tool registry, when the agent lists available tools, then web search is not present
- Given an explicit opt-in for web search in the configuration, when the agent lists available tools, then web search is available
