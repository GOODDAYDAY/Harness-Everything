# Core Domain

The core domain provides the foundational infrastructure that every harness run depends on: configuration validation, artifact persistence, checkpoint-based resumption, project context injection, and graceful shutdown.

## Scenarios

| Sub-file | Concern Area | Key Actors |
|---|---|---|
| [llm.md](llm.md) | API resilience, conversation management, context optimization, tool loop | cycle, operator |
| [security.md](security.md) | Workspace containment, path security | file operation |
| [hooks.md](hooks.md) | Verification hooks (syntax, import, static analysis, test, commit) | cycle |

---

# Config Validation

## US-01: As an operator, I need invalid configuration to be rejected at startup so that misconfigured runs fail immediately rather than hours into execution

Every configurable value must be validated the moment it is loaded. Numeric fields must fall within their documented ranges, string fields must not be blank when a value is required, and list fields must contain well-formed entries. This prevents silent misconfiguration from wasting compute or producing misleading results.

### Acceptance Criteria
- Given a maximum output token count below one, when the configuration is loaded, then startup fails with a clear error message
- Given a maximum output token count above the provider's hard ceiling, when the configuration is loaded, then startup fails with a message naming the ceiling and suggesting typical values
- Given a blank model identifier, when the configuration is loaded, then startup fails with a clear error message
- Given a tool turn budget below one, when the configuration is loaded, then startup fails with a clear error message
- Given a tool turn budget that is unreasonably large, when the configuration is loaded, then a warning is logged but startup proceeds
- Given a log verbosity value that is not a recognized level name, when the configuration is loaded, then startup fails listing the valid options

## US-02: As an operator, I need unknown configuration keys to be rejected so that typos in config files are caught instead of silently ignored

When loading configuration from a dictionary (parsed from JSON or YAML), any key that does not correspond to a known setting must cause an immediate error. This prevents operators from believing a setting is active when it was simply misspelled.

### Acceptance Criteria
- Given a config dictionary containing a key that matches no known setting, when the configuration is built from that dictionary, then an error is raised listing the unknown key(s) and all valid keys
- Given a config dictionary containing comment-style keys (prefixed with `//` or `_`), when the configuration is built, then those keys are silently stripped before validation

## US-03: As an operator, I need the workspace path to be validated at startup so that a non-existent or non-directory path is caught before any work begins

The configured workspace must exist and be a directory. Running against a missing or file-type path would cause every subsequent file operation to fail with confusing errors.

### Acceptance Criteria
- Given a workspace path that does not exist on disk, when the configuration is loaded, then startup fails with an error naming the path
- Given a workspace path that points to a regular file rather than a directory, when the configuration is loaded, then startup fails with an error naming the path
- Given a valid workspace directory, when the configuration is loaded, then all relative paths within the configuration are resolved to absolute paths anchored to that directory

## US-04: As an operator, I need a model identifier without a provider routing prefix to trigger a warning so that I am alerted before a confusing authentication failure at runtime

The system uses a routing layer that requires provider-prefixed model identifiers. A bare model name will authenticate against the wrong endpoint, producing an error that looks like a credentials problem rather than a naming problem.

### Acceptance Criteria
- Given a model identifier that looks like a well-known model name but contains no routing prefix, when the configuration is loaded, then a warning is logged suggesting the correct prefixed forms
- Given a model identifier that already contains a routing prefix, when the configuration is loaded, then no warning is emitted

## US-05: As an operator, I need a structured startup banner emitted at the beginning of every run so that I can identify run boundaries and audit configuration in long log files

A single, grep-friendly line summarizing the active configuration must appear in the log at the start of each run. This lets operators find where each run begins and verify which settings were in effect without opening config files.

### Acceptance Criteria
- Given a fully loaded configuration, when the startup banner is requested, then a single-line string is returned containing the model, output token limit, workspace path, tool turn budget, tool allowlist summary, and log verbosity
- Given an empty tool allowlist (meaning all tools are available), when the startup banner is requested, then the tools field shows "all"

## US-06: As an operator, I need the configured log verbosity applied to the harness logger hierarchy at startup so that all components respect the chosen verbosity without affecting the host application's logging

Log verbosity must be scoped to the harness package only, leaving the caller's own logging configuration untouched.

### Acceptance Criteria
- Given a configured log verbosity, when it is applied at startup, then all loggers within the harness package hierarchy inherit that level
- Given a configured log verbosity, when it is applied at startup, then the root logger and loggers outside the harness package are not affected

---

# Artifact Storage

## US-07: As the agent, I need a hierarchical artifact directory created for each run so that all outputs are organized by round, phase, and iteration for later review

Every run produces artifacts at multiple nesting levels (rounds contain phases, phases contain iterations). The storage must create directories on demand, following a predictable naming convention that makes it easy to navigate outputs after the fact.

### Acceptance Criteria
- Given a new run, when the artifact store is initialized, then a timestamped run directory is created under the configured base directory
- Given a request to write an artifact at a specific nesting path, when the write occurs, then all intermediate directories are created automatically
- Given a request to read an artifact that does not exist, when the read occurs, then an empty string is returned rather than an error

## US-08: As the agent, I need a run-completion marker so that completed runs are distinguishable from interrupted ones

A final summary file serves as both the run's closing output and a machine-readable signal that the run finished successfully. This is essential for the resume logic to distinguish "crashed mid-run" from "completed normally."

### Acceptance Criteria
- Given a run that has completed successfully, when the final summary is written, then the run is reported as complete
- Given a run that was interrupted before the final summary was written, then the run is reported as incomplete

## US-09: As the agent, I need to find and resume the most recent incomplete run so that a crash or interruption does not force a full restart from scratch

When an agent starts up and finds an existing output directory, it should look for the most recent run that started but did not finish, and continue from where it left off rather than starting fresh.

### Acceptance Criteria
- Given an output directory containing multiple run directories where the most recent one lacks a completion marker, when resume detection runs, then that incomplete run is returned
- Given an output directory where all runs have completion markers, when resume detection runs, then no resumable run is found
- Given an output directory that does not exist, when resume detection runs, then no resumable run is found

---

# Checkpoint and Resume

## US-10: As the agent, I need fine-grained done markers for every unit of work so that a resumed run skips exactly the steps that already completed

Each step in the execution hierarchy (iteration, phase, synthesis, meta-review) must have its own completion marker. On resume, the runner checks these markers and skips any step that has already been marked as done.

### Acceptance Criteria
- Given an iteration that completed successfully, when its done marker is written and later queried, then it reports as done
- Given a phase that completed successfully, when its done marker is written and later queried, then it reports as done
- Given a synthesis step that completed, when its done marker is written and later queried, then it reports as done
- Given a meta-review that completed, when its done marker is written and later queried, then it reports as done
- Given an iteration that was never executed, when its done marker is queried, then it reports as not done

## US-11: As the agent, I need the ability to mark a phase as intentionally skipped so that the resume logic distinguishes "skipped by design" from "not yet reached"

Some phases may be skipped based on evaluation results or planning decisions. The checkpoint system must record this distinction so that a resumed run does not attempt to execute an intentionally skipped phase.

### Acceptance Criteria
- Given a phase that was skipped by design, when its skip marker is written and later queried, then it reports as skipped
- Given a phase that was not reached before interruption, when its skip marker is queried, then it reports as not skipped

## US-12: As the agent, I need structured evaluation metadata persisted alongside done markers so that the quality trajectory of a run can be audited after the fact

Beyond the binary done/not-done signal, each checkpoint should record scores, critique counts, and other evaluation metrics so that post-run analysis can trace how quality evolved across iterations.

### Acceptance Criteria
- Given a completed iteration with evaluation scores, when checkpoint metadata is written, then a structured record containing scores, critique counts, and timestamp is persisted alongside the done marker
- Given persisted checkpoint metadata, when it is read back, then all fields are restored to their original values and types
- Given checkpoint metadata with an out-of-range quality score, when it is written, then a validation error is raised

## US-13: As the agent, I need checkpoint path segments validated against directory traversal and injection attacks so that a malicious or buggy input cannot write markers outside the artifact directory

Checkpoint paths are built from round and phase labels that may originate from user-provided configuration. The checkpoint system must reject any segment that could escape the artifact directory.

### Acceptance Criteria
- Given a path segment containing a parent-directory reference, when a done marker write is attempted, then the write is rejected with an error
- Given an empty path segment, when a done marker write is attempted, then the write is rejected with an error
- Given a path segment containing characters that fail security validation, when a done marker write is attempted, then the write is rejected with an error
- Given path segments that would resolve to a location outside the artifact store root, when a done marker write is attempted, then the write is rejected with an error

---

# Project Context

## US-14: As a cycle, I need a compact snapshot of the project's current state injected into the planning prompt so that the planner can reason about what already exists before deciding what to change

The planner needs to know the project's directory layout, recent commit history, current working-tree changes, and file inventory. Without this context, the planner operates blind and may duplicate existing work, conflict with recent changes, or reference non-existent paths.

### Acceptance Criteria
- Given a workspace with a git repository, when the project context is built, then the output contains a section showing recent commits in reverse chronological order
- Given a workspace with uncommitted changes, when the project context is built, then the output contains a section showing the current working-tree status
- Given a workspace directory, when the project context is built, then the output contains a directory tree listing (depth-limited, excluding noise directories like caches and build artifacts)
- Given a workspace with source files, when the project context is built, then the output contains an inventory of key file categories (sources, tests, configs, documentation)

## US-15: As a cycle, I need the project context to be collected in parallel and to degrade gracefully so that context building is fast and never blocks the run

Each piece of context (tree, git log, git status, file inventory) is independent. They should be collected concurrently, and if any one fails (e.g., git is not installed, or a permission error occurs), the remaining pieces should still be included.

### Acceptance Criteria
- Given a workspace without git, when the project context is built, then the git sections are silently omitted and the remaining sections are still present
- Given a workspace where a glob pattern matches no files, when the project context is built, then that category is silently omitted
- Given all context sources available, when the project context is built, then all four sections are collected concurrently rather than sequentially

## US-16: As a cycle, I need the project context output capped at a maximum size so that an unusually large project does not flood the planning prompt and crowd out the actual task

Large projects can produce thousands of lines of tree output and hundreds of file inventory entries. The context block must be truncated to a hard character limit so it remains a useful summary rather than an overwhelming dump.

### Acceptance Criteria
- Given a project whose context exceeds the character limit, when the project context is built, then the output is truncated with a visible marker indicating truncation occurred
- Given a project whose context is within the limit, when the project context is built, then no truncation occurs

---

# Signal Handling

## US-17: As the agent, I need interrupt and termination signals to trigger a graceful shutdown so that the current unit of work can finish and state is saved before the process exits

When an operator presses Ctrl-C or sends a termination signal, the agent should complete its current step (rather than aborting mid-write), persist any in-progress state, and then exit. This prevents corrupted artifacts and half-written checkpoints.

### Acceptance Criteria
- Given an agent running on a Unix-like platform, when an interrupt or termination signal is received, then the registered shutdown callback is invoked
- Given an agent running on a platform that does not support async signal handlers (e.g., Windows), when the signal handler installation is attempted, then the installation is silently skipped and the default interrupt behavior (raising an exception) remains active
- Given that shutdown handlers have been installed, when the agent's work is complete, then the handlers can be cleanly uninstalled to restore default signal behavior
