# Core Domain Requirements

This document defines the requirements for the Harness-Everything core domain: the foundational capabilities that every agent run depends on before any LLM interaction or code transformation begins.

The core domain answers one question: **can the harness start safely, run predictably, and leave a recoverable trail?**

---

## 1. Configuration Validation

### R-CFG-01: Fail-fast validation at startup

All configuration must be validated before the agent begins execution. Every invalid value must produce an actionable error message that names the offending field, the constraint violated, and (where applicable) an example of a correct value.

**Why:** A misconfigured agent can run for hours, consume thousands of API tokens, and produce no usable output before the first failure surfaces. Fail-fast validation catches typos and out-of-range values immediately, before any cost is incurred.

**Acceptance criteria:**

- Given a numeric setting below its minimum, when the configuration is loaded, then startup fails with an error naming the setting and stating the valid range.
- Given a numeric setting above its maximum, when the configuration is loaded, then startup fails with the same specificity.
- Given an empty or whitespace-only model identifier, when the configuration is loaded, then startup fails with an error explaining that a model must be specified.

### R-CFG-02: Workspace existence verification

The configured workspace must be an existing directory on disk. Configuration must not silently accept a path that does not exist or points to a regular file.

**Why:** Every tool invocation resolves file paths relative to the workspace. A nonexistent workspace would cause every file operation to fail with confusing OS-level errors deep inside a tool loop, long after startup.

**Acceptance criteria:**

- Given a workspace path that does not exist, when the configuration is loaded, then startup fails with an error naming the path.
- Given a workspace path that points to a file (not a directory), when the configuration is loaded, then startup fails with a distinct error.

### R-CFG-03: Model identifier format guidance

When the model identifier looks like a bare Anthropic model name without a provider routing prefix, the system must warn the operator.

**Why:** The harness routes LLM calls through LiteLLM, which requires a provider prefix (e.g., `bedrock/`, `anthropic/`). A bare model ID like `claude-sonnet-4-6` silently passes string validation but fails at runtime with an opaque authentication error that wastes debugging time.

**Acceptance criteria:**

- Given a model string like `claude-sonnet-4-6` (no `/`), when the configuration is loaded, then a warning is emitted suggesting the correct prefixed forms.
- Given a model string like `bedrock/claude-sonnet-4-6`, when the configuration is loaded, then no warning is emitted.

### R-CFG-04: Unknown configuration keys are rejected

Configuration loaded from external sources (JSON, YAML, dict) must reject any key that does not correspond to a known setting.

**Why:** A typo like `max_token` (missing `s`) would be silently ignored, and the intended override would never take effect. The operator would see default behavior and spend time debugging the wrong layer.

**Acceptance criteria:**

- Given a configuration dict containing a key `max_token`, when it is loaded, then the system raises an error listing the unknown key and showing all valid keys.
- Given a configuration dict with only known keys, when it is loaded, then no error is raised.

### R-CFG-05: Tool allowlists and denylists reject invalid entries

Lists of tool names and command denylists must not contain empty strings or non-string values. An invalid entry would silently match nothing, giving the operator a false sense that a restriction is in place.

**Why:** A denylist entry of `""` matches no command, so the operator believes `rm` is blocked but it is not. An entry of `None` would cause a type error deep in a tool loop.

**Acceptance criteria:**

- Given an `allowed_tools` list containing `""`, when the configuration is loaded, then startup fails with an error identifying the invalid entry.
- Given a `bash_command_denylist` containing a non-string value, when the configuration is loaded, then startup fails.

### R-CFG-06: Allowed paths outside workspace trigger a warning

When an entry in the allowed-paths list resolves to a location outside the workspace directory, the system must warn the operator.

**Why:** Allowed paths define the security perimeter. An accidental entry outside the workspace silently expands the agent's filesystem access, which may expose sensitive files to LLM-driven tools.

**Acceptance criteria:**

- Given an allowed path `/etc/secrets` and workspace `/home/user/project`, when the configuration is loaded, then a warning is emitted naming both paths.
- Given an allowed path that is a subdirectory of the workspace, when the configuration is loaded, then no warning is emitted.

### R-CFG-07: Log level validation and application

The configured log level must be one of the standard Python logging levels. Once validated, it must be applied to the harness logger hierarchy without disturbing the caller's own logging configuration.

**Why:** An invalid log level (e.g., `VERBOSE`) would silently default to WARNING, hiding diagnostic output the operator expected to see. Applying the level only to the harness namespace prevents the harness from hijacking the host application's log routing.

**Acceptance criteria:**

- Given log level `VERBOSE`, when the configuration is loaded, then startup fails with an error listing valid levels.
- Given log level `debug` (lowercase), when the configuration is loaded, then it is normalized to `DEBUG` and accepted.
- Given log level `DEBUG` applied at startup, then harness loggers emit debug messages but loggers outside the `harness` namespace are unaffected.

---

## 2. Artifact Persistence

### R-ART-01: Hierarchical artifact storage with automatic directory creation

Every artifact write must create any missing parent directories automatically. The directory hierarchy must follow a fixed convention (run / round / phase / inner) so that artifacts from different stages never collide.

**Why:** The agent writes dozens of files per run across multiple rounds and phases. If any write fails because a parent directory is missing, the entire round's output is lost. A flat directory structure would make it impossible to tell which phase produced which file.

**Acceptance criteria:**

- Given a path `round_1/phase_2_dev/inner_3/proposal.txt` that does not yet exist, when an artifact is written, then all intermediate directories are created and the file is written successfully.
- Given two writes to the same path, when the second write occurs, then the file is overwritten (not appended) with the new content.

### R-ART-02: Run completion marker

A run must have a clearly defined completion signal: the presence of a specific marker file at the run root. This marker doubles as the content of the final summary.

**Why:** External monitoring tools, the resume system, and operators all need a single, unambiguous way to determine whether a run finished. Checking for "the last expected file" is fragile because the set of expected files varies by configuration.

**Acceptance criteria:**

- Given a run that completes successfully, when the final summary is written, then the run reports itself as complete.
- Given a run that was interrupted before the final summary, when the completion status is queried, then it reports incomplete.

### R-ART-03: Resumable run discovery

When starting up, the system must be able to find the most recent run that started but did not complete, so work can resume without re-executing finished steps.

**Why:** Agent runs are expensive (hours of LLM calls). A network blip or operator Ctrl-C should not force a full restart from scratch.

**Acceptance criteria:**

- Given a base directory containing one complete run and one incomplete run (has rounds but no final summary), when resumable discovery is invoked, then the incomplete run is returned.
- Given a base directory where all runs are complete, when resumable discovery is invoked, then no run is returned.
- Given a base directory that does not exist, when resumable discovery is invoked, then no run is returned (no crash).

---

## 3. Checkpoint and Resume

### R-CHK-01: Idempotent checkpoint markers

Each completed step must be marked with a zero-cost marker file. Querying whether a step is done must be a simple file-existence check with no parsing or deserialization.

**Why:** The resume system must be fast and reliable. A marker that requires JSON parsing could fail on corruption; a marker that requires querying an external service could fail on network issues. A zero-byte file is atomic on all filesystems and survives unclean shutdown.

**Acceptance criteria:**

- Given a phase that has been marked done, when the system restarts, then the checkpoint query returns true and the phase is skipped.
- Given a phase that was never marked, when the checkpoint query runs, then it returns false.

### R-CHK-02: Skip markers for intentionally omitted steps

The system must distinguish between "completed successfully" and "skipped intentionally" so that reports and monitoring can differentiate the two.

**Why:** A skipped phase (e.g., tests skipped because no test files exist) should not count as a success in quality metrics, and a completed phase should not be re-examined as if it were skipped.

**Acceptance criteria:**

- Given a phase marked as skipped, when completion is queried, then it reports not-done (skipped is not the same as done).
- Given a phase marked as skipped, when skip status is queried, then it reports skipped.

### R-CHK-03: Checkpoint path security

Checkpoint markers must not be writable to arbitrary locations. Path segments used to construct checkpoint paths must be validated against directory traversal attacks and restricted to the artifact store's run directory.

**Why:** Checkpoint paths are constructed from round/phase labels that could originate from LLM output. A malicious or buggy label like `../../etc` could write markers outside the artifact store, corrupting the filesystem or creating false completion signals.

**Acceptance criteria:**

- Given a path segment containing `..`, when a checkpoint marker is written, then the operation fails with a clear error.
- Given a path segment containing a null byte, when a checkpoint marker is written, then the operation fails.
- Given a valid path that resolves outside the run directory, when a checkpoint marker is written, then the operation fails.

### R-CHK-04: Structured checkpoint metadata

Beyond the binary done/not-done marker, checkpoints must support storing structured evaluation metadata (scores, critique counts, timestamps) for downstream analysis.

**Why:** Operators and monitoring dashboards need to see not just whether a step ran, but how well it performed. Without structured metadata, this information is scattered across log files and artifact text.

**Acceptance criteria:**

- Given a checkpoint with metadata including scores and critique counts, when the metadata is written and read back, then all fields round-trip correctly.
- Given metadata with a score value outside the valid range, when it is written, then the operation fails with a validation error.
- Given a checkpoint where the metadata file is missing or corrupt, when metadata is read, then the system returns a "not available" result (no crash).

---

## 4. Project Context Injection

### R-CTX-01: Lightweight project snapshot for LLM orientation

Before planning begins, the system must collect a compact snapshot of the project's current state: directory layout, recent git history, working-tree changes, and key file categories. This snapshot must be formatted for LLM consumption and bounded in size.

**Why:** Without project context, the LLM operates blind -- it doesn't know what files exist, what recently changed, or what the project structure looks like. This leads to hallucinated file paths, duplicated work, and plans that conflict with recent commits.

**Acceptance criteria:**

- Given a workspace that is a git repository, when the context is built, then the output includes recent commit messages, working-tree status, a directory tree, and a file inventory.
- Given a workspace that is NOT a git repository, when the context is built, then git sections are silently omitted and the output still contains the directory tree and file inventory.
- Given any workspace, when the context is built, then the total output never exceeds a fixed character limit (preventing prompt flooding).

### R-CTX-02: Parallel collection with timeout protection

Context collection must gather information from multiple sources (git, filesystem) concurrently, and each source must be individually protected by a timeout.

**Why:** A hung `git log` on a large repository or a slow NFS-mounted workspace must not block the entire harness startup. Each data source should fail independently.

**Acceptance criteria:**

- Given a git command that hangs for more than the timeout, when context is built, then the git section is omitted and the rest of the context is returned normally.
- Given a workspace on a slow filesystem, when context is built, then the tree builder completes within its own timeout boundary.

### R-CTX-03: Noise filtering in directory tree

The directory tree must exclude common noise directories (`.git`, `__pycache__`, `node_modules`, virtual environments) and hidden files. The tree must be depth-limited and entry-limited to prevent explosion on large repositories.

**Why:** A full recursive listing of a project with `node_modules` can be hundreds of thousands of entries. Injecting that into a prompt wastes context window space and drowns the signal (actual project files) in noise.

**Acceptance criteria:**

- Given a workspace containing `.git/`, `__pycache__/`, and `node_modules/`, when the tree is built, then none of these directories appear in the output.
- Given a workspace with more entries than the cap, when the tree is built, then the output is truncated with a visible indicator.

---

## 5. Signal Handling and Graceful Shutdown

### R-SIG-01: Graceful shutdown on interrupt signals

When the operator sends SIGINT (Ctrl-C) or SIGTERM, the system must finish the current unit of work (the in-flight LLM call or tool execution) and then exit cleanly, rather than terminating immediately and leaving partial state.

**Why:** An immediate kill during a file write can leave a half-written file. An immediate kill during an API call wastes the tokens already consumed. Graceful shutdown ensures checkpoints are written and artifacts are consistent.

**Acceptance criteria:**

- Given a running agent loop, when SIGINT is received, then the current tool call completes and the loop exits without crashing.
- Given a running agent loop, when SIGTERM is received, then the same graceful behavior occurs.

### R-SIG-02: Platform compatibility for signal handling

Signal handler installation must degrade gracefully on platforms that do not support asyncio signal handlers (Windows). The system must not crash on unsupported platforms.

**Why:** Developers may run the harness locally on Windows for testing. A crash on import or startup due to signal handling would prevent any use, even though the core functionality (LLM calls, file tools) works fine on all platforms.

**Acceptance criteria:**

- Given a platform that does not support `add_signal_handler`, when signal handlers are installed, then the operation is silently skipped (no crash, no error log).
- Given a platform that does support signal handlers, when they are installed and later uninstalled, then default signal behavior is restored.

### R-SIG-03: Handler cleanup on completion

Signal handlers must be removable so that the event loop can return to default behavior after the agent run completes. Lingering handlers could interfere with subsequent code in the same process.

**Why:** When the harness is used as a library (embedded in a larger application), custom signal handlers that outlive the agent run would intercept signals intended for the host application.

**Acceptance criteria:**

- Given signal handlers that were installed, when uninstall is called, then SIGINT reverts to its default behavior (KeyboardInterrupt).
- Given signal handlers that were never installed (e.g., on Windows), when uninstall is called, then nothing happens (no crash).
