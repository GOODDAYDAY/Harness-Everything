# CI/CD

User stories covering the self-update deployment pipeline, rollback, configuration management, pause/resume mechanisms, auto-tagging, and shell command safety.

---

## US-01: As the CI pipeline, I need to deploy new code to the server whenever a release tag is pushed so that the agent always runs the latest version of itself

The agent improves its own codebase, commits, and tags. Each pushed tag must trigger an automated deployment that brings the running server up to date with the newly committed code. This closes the self-improvement loop: the agent writes better code, and then runs on that better code.

### Acceptance Criteria
- Given a tag matching the release naming convention is pushed to the repository, when the CI pipeline detects the push event, then a deployment job is triggered
- Given a tag that does not match the release naming convention is pushed, when the CI pipeline evaluates the event, then no deployment is triggered
- Given multiple tags are pushed in quick succession, when the CI pipeline processes them, then deployments are serialized (not run in parallel) to avoid race conditions on the server

---

## US-02: As the CI pipeline, I need to run a smoke test on the deployed code before restarting the service so that broken code is caught before it takes over the running agent

The agent is autonomous and self-modifying. Without a verification gate, a single bad commit could deploy code that crashes on startup, wasting the entire next work chunk. The smoke test must verify that the code is syntactically valid and that the configuration can be loaded.

### Acceptance Criteria
- Given the new code has been fetched to the server, when the smoke test runs, then it verifies that all source files compile without syntax errors
- Given the new code has been fetched, when the smoke test runs, then it verifies that the server configuration file loads and parses successfully
- Given the smoke test passes, when the pipeline proceeds, then the service is restarted on the new code
- Given the smoke test fails, when the pipeline evaluates the result, then the service is NOT restarted and the pipeline proceeds to the rollback step

---

## US-03: As the CI pipeline, I need to maintain a "last known good" marker that advances only after a successful smoke test so that rollback always has a safe target

If a deployment fails the smoke test, the server must revert to the most recent code version that was verified to work. A persistent marker (advanced only on success) provides this anchor point.

### Acceptance Criteria
- Given the smoke test passes, when the pipeline completes the verification step, then the last-known-good marker is updated to point at the current release
- Given the smoke test fails, when the pipeline evaluates the result, then the last-known-good marker is NOT updated (it retains its previous value)
- Given the last-known-good marker exists, when a rollback is needed, then the server code is reset to the version indicated by the marker

---

## US-04: As the CI pipeline, I need to automatically roll back the server to the last known good version when a smoke test fails so that a bad self-improvement cycle does not break the loop

A failed smoke test means the latest code is defective. The server must revert to the last verified-good version immediately, without waiting for human intervention, to minimize downtime in the autonomous loop.

### Acceptance Criteria
- Given the smoke test failed and the last-known-good marker exists, when the rollback step runs, then the server code is reset to the last-known-good version
- Given the smoke test failed and no last-known-good marker exists (first-ever deployment was bad), when the rollback step runs, then the pipeline aborts with a clear error message indicating that manual intervention is required
- Given a rollback occurs, when the pipeline completes, then the overall pipeline is marked as failed so the event is visible in CI dashboards and notifications

---

## US-05: As the CI pipeline, I need to propagate the tracked configuration template to the server's active configuration file during each deployment so that configuration changes committed to the repository take effect automatically

The server's active configuration file is not checked into version control (it may contain environment-specific paths). A tracked template in the repository serves as the canonical source. Each deployment copies the template to the active location, ensuring that any configuration changes the agent or operator commits propagate to the running instance.

### Acceptance Criteria
- Given the repository contains an updated configuration template, when the deployment runs, then the active server configuration file is overwritten with the template contents
- Given the active configuration file did not previously exist, when the deployment runs, then the file is created from the template
- Given the template has not changed, when the deployment runs, then the copy still executes (idempotent -- the result is the same file)

---

## US-06: As the CI pipeline, I need to synchronize the server's local branch to match the deployed tag so that the agent's subsequent auto-push operations target a real branch rather than a detached state

The deployment fetches and checks out a specific tag. If the server's working tree is left in a detached state, the agent's auto-push at the end of its next work chunk would fail because there is no branch to push. The deployment must ensure the local branch tracks the deployed tag.

### Acceptance Criteria
- Given a new tag is being deployed, when the server code is updated, then the local main branch is reset to match the tag
- Given the branch reset succeeds, when the agent later commits and pushes, then the push targets the branch (not a detached HEAD) and succeeds

---

## US-07: As the agent, I need to pause between cycles when a pause marker file exists in the workspace so that the operator can temporarily halt autonomous activity without killing the process

Sometimes the operator needs the agent to stop making changes (e.g., to perform manual maintenance or review recent work) but does not want to terminate the process. A file-based pause signal lets the agent finish its current cycle cleanly and then wait until the signal is removed.

### Acceptance Criteria
- Given the pause marker file exists, when the agent finishes a cycle, then it enters a polling wait state and does not start the next cycle
- Given the agent is in the paused wait state and the pause marker file is removed, then the agent resumes and starts the next cycle
- Given the agent is in the paused wait state and a shutdown signal is received, then the agent exits cleanly rather than waiting indefinitely for the file to be removed
- Given the pause marker file does not exist, when the agent finishes a cycle, then it proceeds to the next cycle immediately

---

## US-08: As the CI pipeline, I need to honor a stop marker file that prevents the service from restarting after a deployment so that the operator can halt the self-improvement loop at the next natural boundary

The pause file (US-07) works within a running work chunk. The stop marker works between chunks: it tells the CI pipeline not to restart the service after deployment. This lets the operator schedule a graceful stop at the end of the current chunk without needing to be present at the exact moment the chunk finishes.

### Acceptance Criteria
- Given the stop marker file exists on the server, when the deployment pipeline reaches the service restart step, then it skips the restart, removes the marker, and logs that the loop is paused
- Given the stop marker file does not exist, when the deployment pipeline reaches the restart step, then it restarts the service normally
- Given the stop marker was consumed (removed after skipping restart), when the operator later wants to resume the loop, then they must manually start the service (the marker is a one-shot mechanism)
- Note: the service restart step currently runs unconditionally regardless of smoke test outcome — after a rollback, the service is still restarted on the rolled-back code

---

## US-09: As the agent, I need to automatically create a release tag at periodic checkpoints and push it to the remote so that each chunk of work triggers the self-update deployment pipeline

The deployment pipeline is triggered by pushed release tags. The agent must create and push these tags at regular intervals (configured as a checkpoint action) to close the self-improvement loop. Without auto-tagging, completed work would sit un-deployed until a human intervenes.

### Acceptance Criteria
- Given auto-tagging is enabled and the configured checkpoint interval is reached, when the checkpoint runs, then a new tag with a sequential name is created at the current commit
- Given the tag is created and tag-pushing is enabled, when the tag is created, then it is pushed to the configured remote, triggering the deployment pipeline
- Given auto-tagging is disabled in configuration, when a checkpoint runs, then no tag is created

---

## US-10: As the agent, I need to automatically commit and push changes at the end of each cycle so that work is persisted and available for deployment before the process potentially crashes

Each cycle produces code changes. If those changes are not committed and pushed promptly, a subsequent crash would lose them. Auto-commit and auto-push ensure that every completed cycle's work is safely stored in the remote repository.

### Acceptance Criteria
- Given auto-commit is enabled and a cycle produced changes, when the cycle completes, then all staged changes are committed with a descriptive message
- Given auto-push is enabled and a commit was made, when the commit succeeds, then the current branch is pushed to the configured remote
- Given the push fails (network issue, conflict), when the push attempt returns an error, then the failure is logged but does not crash the agent -- the next cycle can retry

---

## US-11: As the operator, I need a configurable denylist of shell commands that the agent is forbidden from executing so that dangerous operations are blocked even if the LLM decides to attempt them

The agent has shell access for builds, tests, and package management. But certain commands (file deletion, system shutdown, raw disk operations) must never be executed by an autonomous agent. A configurable denylist provides a hard safety boundary that the LLM cannot override.

### Acceptance Criteria
- Given a command's leading program name matches an entry on the denylist, when the agent attempts to execute it, then the command is rejected with a clear permission error before any subprocess is created
- Given a command uses shell chaining operators to hide a denied command after an allowed one, when the agent attempts to execute it, then every segment of the chained command is checked and the denied segment is caught
- Given a command uses an absolute path to a denied program, when the agent attempts to execute it, then the path prefix is stripped and the base program name is matched against the denylist
- Given the denylist is empty, when the agent executes any command, then no commands are blocked

> **Known gap:** The denylist inspects the leading token of each shell-chained segment, but does not inspect arguments or nested command substitutions. A denied command embedded inside a substitution expression (e.g., passed as an argument via shell expansion) would bypass the check. This is a known limitation documented in the codebase.

---

## US-12: As the operator, I need the deployment pipeline to fail visibly when a smoke test fails so that the bad release is surfaced in CI dashboards and notifications

A silent rollback that shows as a green pipeline would mask problems. The operator needs to know that a release was bad so they can investigate the defective code before more tags are pushed.

### Acceptance Criteria
- Given a smoke test fails and rollback completes, when the pipeline finishes, then the overall pipeline status is "failed"
- Given the pipeline fails, when the CI system evaluates the result, then the failure is visible in dashboards and triggers any configured notifications
- Given the pipeline failure message, when the operator reads it, then it identifies the specific tag that failed and advises investigation before pushing more releases
