# Server Operations

This document covers the requirements for running Harness-Everything as a persistent service on a remote server: process management, crash recovery, health monitoring, and disk space management.

---

## Service Management

### Why systemd

The harness is a long-running Python process that must survive server reboots, transient crashes, and bad code deployments. systemd provides process supervision, automatic restart, log management, and user-level service isolation without requiring root access.

### Scenario: Normal service lifecycle

**Context:** The operator deploys Harness-Everything to a server and enables the service.

**Expected behavior:**

1. The service starts after network is available (the harness needs to reach LLM API endpoints and git remotes).
2. The agent process runs `main.py` with a server-specific config JSON. The config file is gitignored and created by the deploy workflow from a tracked template.
3. All stdout and stderr are appended to a single log file. The log file path is fixed and known, not scattered across journald or multiple files. This enables simple `tail -f` monitoring.
4. The agent runs its configured number of cycles, commits, pushes, and tags. When all cycles complete, the process exits cleanly (code 0).
5. After a clean exit, the service remains inactive until the CI pipeline restarts it with the new code. This is intentional -- the agent should not auto-restart on clean exit because the whole point is to load the new code.

**Acceptance criteria:**

- The service is a user-level unit (runs without root, under the deploy user's session)
- The working directory is the repo checkout on the server
- Environment variables (API keys, config overrides) come from a separate environment file, not from the service unit or the repo
- Log output goes to a single rotating file, not to journald alone
- Clean exit (code 0) does not trigger a restart

---

## Crash Recovery

### Why multi-layered recovery

A single retry policy is insufficient for an autonomous self-modifying agent. The agent can crash for transient reasons (API timeout, OOM) or for persistent reasons (the code it just wrote is broken). These require different recovery strategies:

- **Transient failures** should be retried quickly (systemd restart-on-failure).
- **Persistent failures** should be abandoned after a few attempts (systemd start-limit) to avoid burning API credits on a crash loop.
- **Abandoned state** should be recovered by an external monitor (heartbeat cron) that waits, resets the failure counter, and tries again -- because the CI pipeline may have deployed a fix in the meantime.

### Scenario: Transient crash and recovery

**Context:** The agent process crashes mid-cycle due to an API timeout.

**Expected behavior:**

1. systemd detects the non-zero exit code and waits a cooldown period before restarting.
2. The process restarts and begins a fresh run (not a resume -- each run starts from cycle 1 with a new run directory).
3. If the same crash happens again, systemd retries up to a configured limit.
4. After the retry limit is exhausted within a time window, systemd marks the service as failed and stops trying.

**Acceptance criteria:**

- Restart delay is at least 30 seconds to avoid hammering a rate-limited API
- The retry limit is at most 3 attempts within a 10-minute window
- After hitting the limit, the service enters `failed` state and does not restart on its own

### Scenario: Zero-work catastrophe detection

**Context:** The agent process runs, completes its cycles, but makes zero tool calls -- it generated text without actually examining or modifying the codebase. This can happen when the LLM is confused by a broken system prompt or when the API returns malformed responses.

**Expected behavior:**

1. `main.py` detects that cycles ran but total tool calls were zero.
2. The process exits with a non-zero exit code (distinct from both clean exit and unhandled exception) so that systemd treats it as a failure, not a success.
3. This triggers the normal crash recovery path (retry, then heartbeat recovery).

**Acceptance criteria:**

- The exit code for zero-work catastrophe is distinct from normal crash (code 2, not code 1)
- The detection criterion is: at least one cycle completed AND total tool calls across all cycles is zero
- A run where zero cycles completed (immediate startup crash) does not trigger this path -- it triggers normal crash recovery instead

---

## Health Monitoring

### Why a heartbeat exists

systemd's restart-on-failure has a fatal flaw for long-running autonomous systems: the start-limit is permanent until manually reset. If the agent crashes three times in quick succession (e.g., a bad deploy), systemd gives up forever. Without an external monitor, the self-improvement loop halts until a human notices and runs `systemctl reset-failed`.

The heartbeat script is that external monitor.

### Scenario: Recovery from exhausted restart limit

**Context:** The agent crashed three times in 10 minutes. systemd has marked the service as failed and will not restart it. The heartbeat cron runs every 30 minutes.

**Expected behavior:**

1. The heartbeat script checks whether the service is in `failed` state.
2. If failed, it resets the failure counter and attempts a restart.
3. Success or failure of the restart attempt is logged to syslog under a dedicated tag.

**Acceptance criteria:**

- The heartbeat runs on a fixed cron schedule (every 30 minutes)
- It logs every action to syslog with a tag that distinguishes it from the agent's own logs
- It does not restart a service that is actively running (only `failed` state triggers action)
- If the restart attempt itself fails, this is logged but does not cause the heartbeat script to exit with an error (it will try again next cron tick)

### Scenario: Zombie clean-exit detection

**Context:** The agent exited cleanly (code 0) but the last run produced no real work -- the best score across all cycles fell below a safety threshold. The service is in `inactive` state (not `failed`), so systemd will not restart it and the normal crash recovery path does not apply.

**Expected behavior:**

1. The heartbeat script detects the service is `inactive` (not running, not failed).
2. It checks the most recent run's summary to determine the best score.
3. If the best score is below a zombie threshold (a value well below any plausibly healthy run), it restarts the service.
4. If the best score is healthy, the inactive state is normal (waiting for CI to restart with new code) and no action is taken.

**Note:** The current summary is written as Markdown (`final_summary.md`), not JSON. The JSON-based zombie detection logic in the heartbeat script is non-functional (dead code) because no `summary.json` is ever produced.

**Acceptance criteria:**

- The zombie threshold is set low enough that a run with even one productive cycle would not trigger it (a threshold of 1.0 when healthy runs score 5+ is appropriate)
- The script attempts to read a summary from the most recent run directory (sorted by modification time). The current implementation looks for a JSON summary that the agent does not produce -- the agent writes `final_summary.md` instead.
- If no summary file exists, no action is taken (the run may still be in progress in a different form)
- The detection is defensive: any failure to read or parse the summary results in no action, not a crash

---

## Disk Management

### Why automated cleanup

Each run produces a `run_*` directory in the output folder containing cycle artifacts, evaluation results, and metrics. These are valuable for debugging recent runs but become dead weight after a week. Without cleanup, the output directory grows by 1-2 GB per day and will exhaust disk space within weeks on a typical cloud instance.

### Scenario: Routine cleanup of old runs

**Context:** The cleanup script runs daily via cron.

**Expected behavior:**

1. The script finds all `run_*` directories in the output folder older than a retention period.
2. It deletes them entirely (the directories and all contents).
3. It logs the count of deleted directories to syslog.
4. If no directories are old enough, it exits silently.

**Acceptance criteria:**

- The retention period is at least 7 days (enough to investigate recent failures)
- Only directories matching the `run_*` pattern are deleted -- other files in the output directory are untouched
- The search is shallow (maxdepth 1) to avoid accidentally deleting files inside a current run
- If the output directory does not exist, the script exits cleanly without error
- The cleanup runs at a low-traffic time (e.g., 04:00) to avoid contention with active runs
- Deletion count is logged to syslog under a dedicated tag

---

## Environment Setup

### Scenario: Server filesystem layout

**Expected layout:**

1. The repo checkout lives at a fixed path on the server.
2. A Python virtual environment exists outside the repo (not committed, not inside the workspace).
3. An environment file containing API keys and other secrets lives outside the repo, in a user config directory.
4. The server-specific agent config JSON is gitignored. The deploy workflow creates it by copying a tracked template.
5. A logs directory exists for the service's output file.

**Acceptance criteria:**

- The virtual environment, environment file, and server config are all outside the git working tree
- The environment file is readable only by the deploy user (not world-readable)
- The deploy workflow does not assume the virtual environment or environment file exist -- it only manages the repo checkout and the config copy
