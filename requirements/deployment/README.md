# Deployment Domain

The deployment domain governs how Harness-Everything runs as a persistent, self-updating service on a remote server. This is not a typical web application deployment -- it is an autonomous agent that modifies its own source code, commits the changes, and triggers its own redeployment. The deployment system must handle this self-referential lifecycle without human intervention while maintaining the ability for a human to pause, inspect, and resume at any time.

## Why deployment is its own domain

The agent loop (covered in the agent domain) manages what happens within a single process invocation. But a single process cannot apply changes to its own Python source -- modules are loaded once at startup. The deployment domain solves this by orchestrating the boundary between process generations: one generation writes code, commits, pushes, and tags; the CI system validates the new code and restarts the process so the next generation loads the improved modules.

## Scope

This domain covers three concern areas:

| Concern | Document | Core question |
|---------|----------|---------------|
| Server operations | [server-operations.md](server-operations.md) | How does the service run, survive crashes, and manage disk space? |
| CI/CD pipeline | [ci-cd.md](ci-cd.md) | How do code changes flow from agent commit to validated deployment? |

## Key actors

- **Agent** -- the Python process running `main.py` with a config JSON. It writes code, commits, pushes, and tags. It does not manage its own process lifecycle.
- **systemd** -- the service manager that starts, stops, and restarts the agent process. It enforces crash recovery policy.
- **Heartbeat cron** -- a periodic script that detects when systemd has given up (3-strike failure limit) or when the agent exited cleanly but produced no useful work (zombie exit). It resets the failure counter and restarts the service.
- **Cleanup cron** -- a periodic script that deletes old run output directories to prevent disk exhaustion.
- **GitHub Actions** -- the CI system that responds to release tags, validates new code on the server, and either advances the deployment or rolls back to the last known-good tag.
- **Operator** -- the human who can pause the loop (via a stop marker file), inspect logs, and resume by removing the marker or pushing a new tag.

## Lifecycle overview

```
Agent runs N cycles
    |
    v
Commits + pushes to main + creates release tag (harness-r*)
    |
    v
GitHub Actions triggered by tag
    |
    v
SSH to server --> git fetch + reset to new tag
    |
    v
Smoke test (py_compile + config validation)
    |
    +-- PASS --> advance harness-last-good tag, restart service
    |
    +-- FAIL --> rollback to harness-last-good tag, restart service, fail workflow
    |
    v
Agent starts again with updated code
```

This cycle repeats indefinitely. Each generation of the agent produces the next generation's code. The CI pipeline is the quality gate that prevents a bad generation from permanently breaking the loop.

## Cross-cutting constraints

1. **No single failure should permanently halt the loop.** Crash recovery, heartbeat monitoring, and CI rollback all exist to ensure that a bad cycle, a bad deploy, or a transient error results in at most a temporary interruption, not a permanent outage.

2. **Human override must be instantaneous and reversible.** The operator can pause the loop by creating a stop marker file. Removing the file (or pushing a new tag) resumes it. No config changes, no service file edits, no SSH gymnastics required.

3. **Disk space is finite.** Each run produces 10-50 MB of output. At one run per 30 minutes, that is 1-2 GB per day. Without cleanup, the disk fills within weeks. Automated cleanup is not optional.

4. **Secrets never appear in tracked files.** API keys come from an environment file outside the repo. Config templates in the repo use empty `api_key` fields. The deploy workflow copies a tracked template to a gitignored server config, and the environment file provides the key at runtime.
