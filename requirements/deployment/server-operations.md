# Server Operations

User stories covering the systemd service lifecycle, crash recovery, health monitoring, and disk cleanup.

---

## US-01: As the operator, I need the agent to run as a managed background service so that it starts automatically on boot and can be controlled with standard system commands

The self-improvement loop must run as a long-lived background process managed by the operating system's service supervisor. This ensures the loop survives terminal disconnections, starts on reboot, and provides a uniform interface for start/stop/status operations.

### Acceptance Criteria
- Given the service is enabled, when the server boots, then the agent process starts automatically without manual intervention
- Given the service is running, when the operator issues a stop command, then the agent process terminates within a bounded grace period
- Given the service is stopped, when the operator issues a start command, then the agent process launches and begins executing work cycles

---

## US-02: As the operator, I need the service to wait for network availability before starting so that the agent can reach its LLM provider and git remote

The agent depends on external network services (LLM API, git remote). Starting before the network is ready would cause immediate failures and waste retry budget.

### Acceptance Criteria
- Given the server is booting, when the network is not yet available, then the service delays its start until connectivity is established
- Given the network becomes available, when the service starts, then the agent can successfully reach external APIs on its first attempt

---

## US-03: As the operator, I need the agent process to read its credentials from a separate environment file so that secrets are decoupled from the service definition and the codebase

API keys and other secrets must not live in the service unit file, the source repository, or the agent config file. A dedicated external file keeps secrets out of version control and allows rotation without redeploying code.

### Acceptance Criteria
- Given the environment file exists with valid credentials, when the service starts, then the agent authenticates successfully using those credentials
- Given the environment file is missing or malformed, when the service starts, then the agent fails immediately with a clear error rather than running unauthenticated

---

## US-04: As the operator, I need the service to redirect all output to a rotating log file so that diagnostic history is preserved without filling system journal storage

The agent produces continuous output across many hours. Writing to the system journal would accumulate unbounded storage. A dedicated log file can be size-managed independently.

### Acceptance Criteria
- Given the service is running, when the agent produces stdout or stderr output, then that output is appended to the designated log file rather than the system journal
- Given the log file exists, when new output is written, then it is appended (not overwritten) so that previous entries are preserved

---

## US-05: As the operator, I need the service to automatically retry on crash with a bounded number of attempts so that transient failures self-heal but persistent bad code does not loop forever

A single crash (out-of-memory, transient API error) should not stop the loop permanently. But if the agent crashes repeatedly in quick succession, it likely has a code defect, and unlimited restarts would waste resources and fill logs with identical failures.

### Acceptance Criteria
- Given the agent process crashes once, when the service supervisor detects the failure, then it waits a cooldown period and restarts the process
- Given the agent process crashes repeatedly, when the number of crashes within the rate-limit window exceeds the allowed burst count, then the supervisor stops attempting restarts and marks the service as failed
- Given the agent exits cleanly (success code), when the service supervisor evaluates the exit, then no automatic restart occurs because a clean exit means the work chunk is complete

---

## US-06: As the operator, I need the agent to signal a catastrophic exit when it completes cycles but produces no actual work so that the service supervisor treats this as a failure requiring recovery

An agent run that finishes its cycles without making a single tool call is effectively broken -- it consumed time and API credits but accomplished nothing. This must be distinguishable from a legitimate clean exit so the crash-recovery mechanisms can intervene.

### Acceptance Criteria
- Given the agent finishes at least one cycle, when zero tool calls were made across all cycles, then the process exits with a failure code
- Given the agent finishes cycles with at least one tool call, when the process exits, then it uses the success code regardless of whether the changes were large or small
- Given the agent exits with the catastrophic failure code, when the service supervisor evaluates the exit, then it counts this toward the crash retry budget (same as a genuine crash)

---

## US-07: As the operator, I need a scheduled health check that automatically recovers the service after it exhausts its crash retry budget so that a brief period of bad code does not halt the loop for hours

Once the service supervisor gives up after repeated crashes, nothing else will restart the service until a human intervenes. A periodic health check must detect this "failed and stuck" state and attempt recovery, because the next deployed code version may have already fixed the underlying defect.

### Acceptance Criteria
- Given the service is in a failed state (crash budget exhausted), when the health check runs, then it resets the failure counter and attempts to start the service
- Given the health check successfully restarts the service, when the restart completes, then the event is logged to the system log for auditability
- Given the health check fails to restart the service, when the restart attempt fails, then the failure is logged and the health check exits without further action (the next scheduled run will try again)

---

## US-08: As the operator, I need the health check to detect "zombie clean exits" where the service stopped normally but the last run produced no meaningful work so that the loop resumes even when the failure bypasses the crash-retry mechanism

A zero-work exit is supposed to use a failure exit code (see US-06), but defense in depth requires a second detection path. If a code regression causes zero-work runs to exit with a success code, the service stops cleanly and the crash-retry mechanism never fires. The health check must independently detect this and restart the service.

### Acceptance Criteria
- Given the service is inactive (not failed) and the most recent run output indicates a quality score below the minimum threshold, when the health check runs, then it restarts the service
- Given the service is inactive and no run output exists, when the health check runs, then it takes no action (there is nothing to evaluate)
- Given the service is actively running, when the health check runs, then it takes no action

> **Known issue:** The zombie detection branch looks for a JSON summary file in the run output directory. However, the agent currently writes Markdown-format summaries, not JSON. This means the zombie detection path is effectively dead code -- it will never find a matching file, and the branch will never fire. The primary defense (failure exit code from US-06) remains functional.

---

## US-09: As the operator, I need old run output directories to be automatically deleted after a retention period so that the server disk does not fill up over days of continuous operation

Each completed run produces tens of megabytes of output. With the loop firing roughly every 30 minutes, output accumulates at 1-2 GB per day. Without cleanup, the disk fills within weeks.

### Acceptance Criteria
- Given run output directories older than the retention period exist, when the cleanup job runs, then those directories are deleted and the count is logged
- Given no run output directories exceed the retention period, when the cleanup job runs, then no directories are deleted and no log entry is created
- Given the output root directory does not exist, when the cleanup job runs, then it exits silently without error

---

## US-10: As the operator, I need the cleanup job to run on a daily schedule so that disk usage stays bounded without manual intervention

The cleanup must be automated and periodic. Running it once per day is sufficient because the daily accumulation rate is manageable and a single missed run would not cause immediate disk pressure.

### Acceptance Criteria
- Given the cleanup job is scheduled, when the scheduled time arrives each day, then the cleanup executes automatically
- Given the cleanup job was missed (server was down), when the server comes back up and the next scheduled time arrives, then the cleanup runs and handles the accumulated backlog in a single pass
