# DEP-03: Run Cleanup

Status: **Active**
Source: `deploy/cleanup_runs.sh`

---

## Cleanup Script

File: `deploy/cleanup_runs.sh`

### Purpose

Deletes `harness_output/run_*` directories older than 7 days to prevent the disk from filling up. Each run directory is approximately 10-50 MB; with the loop firing every ~30 minutes, that accumulates to approximately 1-2 GB/day if not cleaned.

### Installation

Cron schedule: daily at 04:00

```
0 4 * * * /home/ubuntu/harness-everything/deploy/cleanup_runs.sh
```

### Logging

Logs to syslog under tag `harness-cleanup`.

### Shell settings

- `set -u` -- treat unset variables as errors

---

## Configuration

| Variable | Value | Notes |
|----------|-------|-------|
| `OUT_DIR` | `/home/ubuntu/harness-everything/harness_output` | Output directory |
| `KEEP_DAYS` | `7` | Retention period in days |

---

## Behaviour

1. If `OUT_DIR` does not exist, exit silently (code 0)
2. Find directories matching `run_*` at the top level of `OUT_DIR` (`-maxdepth 1`) that are older than `KEEP_DAYS` days (`-mtime +7`)
3. Delete matching directories (`rm -rf`) and count them
4. If any directories were deleted, log the count: `"deleted $deleted run_* dirs older than $KEEP_DAYS days"`
5. If none were deleted, exit silently

### find command

```bash
find "$OUT_DIR" -maxdepth 1 -name 'run_*' -type d -mtime "+$KEEP_DAYS" -print -exec rm -rf {} +
```

The `-print` output is piped to `wc -l` to count deleted directories.
