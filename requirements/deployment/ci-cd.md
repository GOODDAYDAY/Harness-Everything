# CI/CD Pipeline

This document covers the requirements for the autonomous deployment cycle: how code changes flow from agent commit to validated deployment, how bad deployments are rolled back, how agent configurations are managed, and how the operator can pause and resume the loop.

---

## Self-Update Loop

### Why the agent cannot apply its own changes

Python loads modules at process startup. When the agent edits `harness/tools/bash.py`, the running process still uses the old version loaded into memory. The only way to apply the change is to restart the process. But the agent cannot safely restart itself -- it would need to validate the new code first, and if validation fails, it would need to roll back, which requires a process that is not itself broken.

The CI pipeline solves this by acting as an external validator. The agent's job ends at "push and tag." The pipeline's job begins at "validate and restart." This separation means a bad code change cannot prevent its own rollback.

### Scenario: Successful deployment cycle

**Context:** The agent has completed its configured number of cycles, committed changes, pushed to the remote, and created a release tag matching the configured pattern.

**Expected behavior:**

1. GitHub Actions detects the new tag (matching a pattern like `harness-r*`).
2. The workflow SSHs to the server and updates the repo checkout to the new tag.
3. A smoke test runs on the server: syntax-check all Python files and validate that the agent config loads without error.
4. On smoke pass, the workflow advances a "last-good" marker tag to the current tag and pushes it. This marker is the rollback target for future failures.
5. The service is restarted, loading the new code.
6. The agent begins a new run with the updated codebase.

**Acceptance criteria:**

- The workflow triggers only on tags matching the configured prefix pattern, not on every push
- Concurrent deployments are serialized (only one deploy runs at a time), and in-progress deploys are never cancelled by a newer one
- The smoke test runs on the actual server (not in the CI runner's environment), using the server's Python environment and the server's config file
- The smoke test validates both syntax (`py_compile` on all harness Python files) and configuration (the agent config loads and its key fields are populated)
- The last-good marker tag is advanced only after the smoke test passes, never before
- The tag is force-pushed (it is a moving marker, not a historical record)

---

## Rollback Safety

### Why automatic rollback is essential

The agent writes its own code. A bad cycle can produce code that compiles but crashes at runtime, or code that breaks a module the smoke test does not cover. Without automatic rollback, a single bad tag would halt the loop until a human investigates. Automatic rollback to the last-good tag keeps the loop running while the bad tag is logged as a workflow failure for later investigation.

### Scenario: Failed smoke test with rollback

**Context:** The agent pushed a tag containing code that fails the smoke test (e.g., a syntax error in a file not covered by the agent's own hooks, or a config schema change that breaks loading).

**Expected behavior:**

1. The smoke test step fails.
2. The workflow SSHs to the server and resets the repo checkout to the last-good marker tag.
3. The service is restarted with the rolled-back code.
4. The workflow exits with a failure status, making the bad tag visible in the GitHub Actions UI.
5. The last-good marker tag is NOT advanced (it still points to the previous good tag).

**Acceptance criteria:**

- Rollback is automatic -- no human intervention required
- The rollback target is always the last-good marker tag, not "the previous tag" (there may be multiple bad tags in sequence)
- If the last-good marker tag does not exist (first-ever deployment was bad), the workflow fails loudly with an explicit message requiring manual intervention. It does not attempt to roll back to an unknown state.
- The workflow failure is clearly marked in the CI UI so the operator can investigate the bad tag
- The service is restarted even after rollback, so the loop continues with the known-good code

---

## Configuration Management

### Why config is separated from code

The agent config JSON contains environment-specific paths, API endpoint URLs, and references to secrets. These differ between the server, local development, and other target projects. Tracking server-specific config in git would leak server paths and API URLs into the public repo. Instead, a sanitised template is tracked, and the deploy workflow copies it to a gitignored server config at deploy time.

### Scenario: Config propagation during deployment

**Context:** An operator (or the agent itself) has updated the tracked config template with a new setting (e.g., adding a tool to `allowed_tools`, changing `max_cycles`).

**Expected behavior:**

1. The template change is committed and included in the next release tag.
2. During deployment, the workflow copies the tracked template to the server config path.
3. The service restarts and picks up the new settings.

**Acceptance criteria:**

- The server config file is gitignored and never committed
- The tracked template contains no secrets (empty `api_key`, sanitised `base_url`, generic `workspace` path)
- The deploy workflow always copies the template fresh -- it does not merge with an existing server config. The template is the single source of truth for config structure.
- API keys and other secrets are provided via the environment file, not the config JSON

### Scenario: Agent tool restrictions

**Context:** The agent config specifies which tools the agent may use and which bash commands are denied.

**What the config must support:**

1. **Tool allowlist.** An explicit list of tool names the agent may invoke. An empty list means "all tools." This lets operators restrict capabilities per-project (e.g., disabling `batch_write` for models that cannot emit its schema correctly).

2. **Bash command denylist.** A list of commands the agent may not execute through the bash tool. This prevents the agent from running destructive commands (`rm`, `shutdown`, `reboot`) or commands that conflict with the harness's own orchestration (`git` -- because the harness manages git operations).

3. **Cycle verification hooks.** A configurable list of post-cycle checks that must pass before a commit is allowed. Different projects need different hooks (e.g., `import_smoke` is inappropriate for projects with heavy native dependencies that fail on import).

**Acceptance criteria:**

- Tool restrictions are enforced at the framework level, not by the LLM's compliance with instructions
- The bash denylist blocks commands when embedded in pipes and chain operators (`&&`, `||`, `;`, `|`). Command substitution (`$(...)` and backticks) is not currently inspected -- this is a known limitation.
- Hook configuration is per-project, not global -- different config files can specify different hook sets

---

## Pause and Resume

### Why manual pause is necessary

The self-improvement loop is designed to run indefinitely, but operators need to inspect, debug, or manually intervene without permanently stopping the service or losing state.

### Scenario: Operator pauses the loop

**Context:** The operator wants to stop the loop after the current chunk finishes (not mid-cycle) to inspect the output.

**Expected behavior:**

1. The operator creates a stop marker file at a known path on the server.
2. The agent finishes its current chunk (N cycles), commits, pushes, and tags as normal.
3. The CI pipeline deploys as normal, but at the restart step, it detects the stop marker.
4. Instead of restarting the service, the pipeline removes the marker and logs that the loop is paused.
5. The service remains stopped. The operator can inspect logs, review commits, and decide whether to continue.

**Acceptance criteria:**

- The stop marker is a simple file presence check, not a config change or API call
- The marker path is fixed and documented, not derived from config
- The marker is consumed (deleted) when detected, so the operator does not need to clean it up before resuming
- The loop pauses at a clean boundary (between chunks), not mid-cycle
- Resuming requires only pushing a new tag or manually starting the service -- no special "resume" command

### Scenario: Operator resumes the loop

**Context:** The operator has finished inspection and wants the loop to continue.

**Expected behavior:**

1. The operator either pushes a new tag (triggering the CI pipeline which will restart the service) or manually starts the service via systemctl.
2. The agent begins a new run from cycle 1 with fresh state.

**Acceptance criteria:**

- No persistent state from the pause survives into the resumed run (each run is independent)
- Both resume methods (new tag push and manual service start) result in the same behavior

### Scenario: In-process pause via pause file

**Context:** The operator (or an external script) wants to pause the agent within a running process without stopping the service or waiting for the current chunk to finish. This complements the stop marker (which pauses between CI chunks) by providing a finer-grained pause within a running process.

**Expected behavior:**

1. The operator creates a `.harness.pause` file in the workspace root.
2. The agent finishes its current cycle (it does not abort mid-cycle).
3. The agent enters a sleep loop, checking periodically for the pause file's removal.
4. When the `.harness.pause` file is removed, the agent resumes execution from the next cycle.

**Acceptance criteria:**

- The pause file is a simple file presence check (`.harness.pause` in the workspace root)
- The agent completes its current cycle before pausing -- it does not interrupt mid-cycle
- The agent resumes automatically when the pause file is removed, without requiring a process restart
- This mechanism operates within a running process, unlike the stop marker which operates between CI-triggered chunks

---

## Auto-Tagging

### Why the agent creates its own release tags

The deployment pipeline is tag-triggered. The agent must create tags automatically at the end of each chunk to close the self-improvement loop. Without auto-tagging, a human would need to create a tag after every chunk, defeating the purpose of autonomous operation.

### Scenario: End-of-chunk tagging

**Context:** The agent has completed its configured number of cycles (e.g., 20). Auto-commit, auto-push, and auto-tag are all enabled.

**Expected behavior:**

1. After the final cycle's commit, the framework creates a tag with a configured prefix and a monotonically increasing identifier (e.g., timestamp).
2. The tag is pushed to the remote.
3. The pushed tag triggers the CI pipeline.

**Acceptance criteria:**

- Tags are created only at the configured interval (e.g., every 20 cycles), not after every cycle
- The tag prefix is configurable and matches the CI pipeline's trigger pattern
- Tag names are unique and monotonically increasing (no collisions, no reuse)
- Tag push uses the same remote and authentication as the regular push
- If auto-push is disabled, auto-tag is also effectively disabled (a tag on a local-only commit cannot trigger CI)
