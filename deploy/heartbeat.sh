#!/bin/bash
# heartbeat.sh — restart harness.service when systemd has given up after the
# 3-strike crash limit (StartLimitBurst).  Without this, a brief bad-code run
# can permanently halt the self-improvement loop because nothing else
# unsticks the "failed" state.
#
# Install via cron: */30 * * * * /home/user/harness-everything/deploy/heartbeat.sh
#
# Logs to syslog under tag harness-heartbeat — view with:
#   journalctl -t harness-heartbeat --since '1 day ago'

set -u

SERVICE="harness.service"
RUNS_DIR="/home/user/harness-everything/harness_output"
# Treat a completed run as "catastrophic" if its best_score fell below this
# threshold. Runs that legitimately early-stop after no-improvement typically
# still have best_score > 5 from earlier productive rounds; 1.0 is well under
# any plausibly-healthy value and unambiguously signals "every phase
# crashed" (see validate_calibration_anchors incident 2026-04-19).
ZOMBIE_BEST_SCORE_THRESHOLD="1.0"

state=$(systemctl --user is-failed "$SERVICE" 2>&1 || true)

if [ "$state" = "failed" ]; then
    systemctl --user reset-failed "$SERVICE"
    if systemctl --user start "$SERVICE"; then
        logger -t harness-heartbeat "service was failed; reset + restarted OK"
    else
        logger -t harness-heartbeat "service was failed; restart attempt FAILED"
    fi
    exit 0
fi

# Zombie clean-exit detection: service exited with status 0 (so it's "inactive",
# not "failed") but the last run produced no real work. harness.cli exits 2 on
# such runs — which lands in the is-failed branch above — but we keep this
# defensive branch so older deployments or a regressed exit-code path still
# recover within one cron tick. One extra restart per broken deploy is cheap;
# a 5-hour outage (as observed 2026-04-19) is not.
if [ "$(systemctl --user is-active "$SERVICE")" = "inactive" ]; then
    LATEST_SUMMARY=$(ls -td "$RUNS_DIR"/run_*/summary.json 2>/dev/null | head -1)
    if [ -n "${LATEST_SUMMARY:-}" ] && [ -f "$LATEST_SUMMARY" ]; then
        BEST=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('best_score', 0.0))" "$LATEST_SUMMARY" 2>/dev/null || echo 0.0)
        if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)" "$BEST" "$ZOMBIE_BEST_SCORE_THRESHOLD" 2>/dev/null; then
            if systemctl --user start "$SERVICE"; then
                logger -t harness-heartbeat "zombie clean-exit detected (best=$BEST, summary=$LATEST_SUMMARY); restarted OK"
            else
                logger -t harness-heartbeat "zombie clean-exit detected (best=$BEST); restart FAILED"
            fi
        fi
    fi
fi
