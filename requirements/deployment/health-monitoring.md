# DEP-02: Health Monitoring

Status: **Active**
Source: `deploy/heartbeat.sh`

---

## Heartbeat Script

File: `deploy/heartbeat.sh`

### Purpose

Restarts `harness.service` when systemd has given up after the 3-strike crash limit (`StartLimitBurst`). Without this, a brief bad-code run can permanently halt the self-improvement loop because nothing else unsticks the "failed" state.

### Installation

Cron schedule: every 30 minutes

```
*/30 * * * * /home/ubuntu/harness-everything/deploy/heartbeat.sh
```

### Logging

Logs to syslog under tag `harness-heartbeat`. View with:
```
journalctl -t harness-heartbeat --since '1 day ago'
```

### Shell settings

- `set -u` -- treat unset variables as errors

---

## Recovery Scenarios

### Scenario 1: Service in "failed" state

**Detection**: `systemctl --user is-failed harness.service` returns `"failed"`

**Recovery steps**:
1. `systemctl --user reset-failed harness.service`
2. `systemctl --user start harness.service`
3. Log result:
   - Success: `"service was failed; reset + restarted OK"`
   - Failure: `"service was failed; restart attempt FAILED"`
4. Exit immediately (`exit 0`) after handling

### Scenario 2: Zombie clean-exit detection

**Detection**: Service exited with status 0 (state is `"inactive"`, not `"failed"`) but the last run produced no real work.

This scenario exists as a defensive fallback. `main.py` now exits with code 2 on zero-work runs (which lands in the "failed" branch above), but this branch covers older deployments or a regressed exit-code path.

**Constants**:

| Variable | Value | Notes |
|----------|-------|-------|
| `SERVICE` | `harness.service` | systemd service name |
| `RUNS_DIR` | `/home/ubuntu/harness-everything/harness_output` | Output directory containing `run_*` dirs |
| `ZOMBIE_BEST_SCORE_THRESHOLD` | `1.0` | Runs with `best_score` below this are treated as catastrophic |

**Threshold rationale**: Runs that legitimately early-stop after no-improvement typically still have `best_score > 5` from earlier productive rounds. A threshold of `1.0` is well under any plausibly-healthy value and unambiguously signals "every phase crashed". Reference incident: `validate_calibration_anchors` incident 2026-04-19.

**Detection steps**:
1. Check `systemctl --user is-active harness.service` returns `"inactive"`
2. Find the latest `summary.json` by sorting `run_*` directories by modification time: `ls -td "$RUNS_DIR"/run_*/summary.json | head -1`
3. Extract `best_score` from the JSON using Python: `json.load(open(path)).get('best_score', 0.0)`, defaulting to `0.0` on error
4. Compare `best_score < ZOMBIE_BEST_SCORE_THRESHOLD` using Python float comparison
5. If below threshold: restart the service

**Recovery log messages**:
- Success: `"zombie clean-exit detected (best=$BEST, summary=$LATEST_SUMMARY); restarted OK"`
- Failure: `"zombie clean-exit detected (best=$BEST); restart FAILED"`

### Scenario 3: Service is running normally

If the service is neither `"failed"` nor `"inactive"` (i.e., it is `"active"`), the script does nothing and exits silently.
