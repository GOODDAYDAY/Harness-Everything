# DEP-05: Self-Update Loop (CI/CD)

Status: **Active**

---

## Overview

The self-update loop enables fully autonomous code improvement: the agent runs N cycles, commits and pushes improvements, auto-tags a release, and a GitHub Actions workflow smoke-tests and redeploys the updated code — then the cycle repeats.

Source: `.github/workflows/deploy.yml` (98 lines)

## Trigger

**F-01** Workflow triggers on tag pushes matching the pattern `harness-r*`.

**F-02** The server agent config sets `auto_tag_interval: 20`, `auto_tag_prefix: "harness-r"`, `auto_tag_push: true`. After 20 cycles, the agent auto-tags (e.g. `harness-r1`, `harness-r2`, ...) and pushes the tag to `origin`, which triggers the workflow.

## Workflow Steps

**F-03** Permissions: `contents: write` (needed to advance `harness-last-good` tag).

**F-04** Concurrency group `harness-deploy` with `cancel-in-progress: false` — deploys queue rather than cancel each other.

**F-05** Step 1 — Checkout with `fetch-depth: 0` and `fetch-tags: true` (full history for tag operations).

**F-06** Step 2 — Setup SSH: writes `secrets.DEPLOY_SSH_KEY` to `~/.ssh/id_ed25519`, adds server host key via `ssh-keyscan`.

**F-07** Step 3 — Smoke test on server (`continue-on-error: true`):
1. SSH to server, `cd /home/ubuntu/harness-everything`
2. `git fetch --all --tags --prune --force`
3. `git checkout main && git reset --hard <tag>` (sync to new tag on a real branch, not detached HEAD)
4. Copy config template: `cp config/agent_example_self_improve_server.json agent_server.json`
5. `py_compile` all Python files under `harness/` and `main.py`
6. Import and validate `AgentConfig` from the copied config (prints `max_cycles` and `auto_tag_interval`)

**F-08** Step 4 — On smoke pass: advance `harness-last-good` tag to the new tag and force-push to origin (runs on the GitHub Actions runner, not the server).

**F-09** Step 5 — On smoke fail: SSH to server and rollback:
1. `git fetch --all --tags --prune --force`
2. Check `harness-last-good` tag exists (abort with error if not — first deploy was bad)
3. `git checkout main && git reset --hard harness-last-good`

**F-10** Step 6 — Restart service (unless stop marker exists):
1. Check for `$HOME/.config/harness/STOP_AFTER_CHUNK` marker file
2. If marker exists: delete it, do NOT restart service, log "Loop paused"
3. If no marker: `systemctl --user restart harness.service`

**F-11** Step 7 — If smoke failed, fail the workflow with `::error::` annotation naming the bad tag and advising investigation.

## STOP_AFTER_CHUNK Marker

**F-12** The `STOP_AFTER_CHUNK` marker at `$HOME/.config/harness/STOP_AFTER_CHUNK` allows an operator to pause the self-update loop:
- Create the file before the current chunk ends
- When the deploy workflow runs, it detects the marker, deletes it, and skips the service restart
- The agent does not start a new chunk; the loop is paused until manually restarted

## End-to-End Flow

```
Agent runs 20 cycles (max_cycles: 20)
  → auto-commits + auto-pushes each cycle
  → at cycle 20, creates tag harness-r<N> and pushes
  → tag push triggers deploy.yml
  → smoke test on server
  → pass: advance harness-last-good, restart service → next 20-cycle chunk
  → fail: rollback to harness-last-good, restart service with known-good code
```

### Config file flow

```
config/agent_example_self_improve_server.json  (tracked template)
        |
        | deploy.yml copies to:
        v
agent_server.json  (server-side active config, gitignored)
```

API key comes from `/home/ubuntu/.config/harness/env` as `HARNESS_API_KEY=...` (EnvironmentFile in harness.service).

## Implementation Approach

- GitHub Actions workflow (`.github/workflows/deploy.yml`)
- SSH-based remote execution to the deployment server
- Tag-based versioning with `harness-last-good` as the rollback anchor
- Smoke test validates both syntax (`py_compile`) and config parsing (`AgentConfig.from_dict`)

## Acceptance Criteria

- AC-01: Tag push matching `harness-r*` triggers the workflow.
- AC-02: Smoke test failure triggers rollback to `harness-last-good` tag.
- AC-03: Smoke test success advances `harness-last-good` to the new tag.
- AC-04: `STOP_AFTER_CHUNK` marker prevents service restart and is consumed (deleted) on detection.
- AC-05: Missing `harness-last-good` tag on first-ever failed deploy produces an actionable error message.
- AC-06: Concurrent deploys queue (do not cancel each other).
