#!/bin/bash
# cleanup_runs.sh — delete harness_output/run_* dirs older than 7 days to
# prevent the disk from filling up.  Each run dir is ~10–50 MB; with the
# loop firing every ~30 min, that's ~1–2 GB/day if not cleaned.
#
# Install via cron: 0 4 * * * /home/ubuntu/harness-everything/deploy/cleanup_runs.sh
#
# Logs to syslog under tag harness-cleanup.

set -u

OUT_DIR="/home/ubuntu/harness-everything/harness_output"
KEEP_DAYS=7

if [ ! -d "$OUT_DIR" ]; then
    exit 0
fi

deleted=$(find "$OUT_DIR" -maxdepth 1 -name 'run_*' -type d -mtime "+$KEEP_DAYS" -print -exec rm -rf {} + | wc -l)
if [ "$deleted" -gt 0 ]; then
    logger -t harness-cleanup "deleted $deleted run_* dirs older than $KEEP_DAYS days"
fi
