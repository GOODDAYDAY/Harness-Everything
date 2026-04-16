#!/bin/bash
# heartbeat.sh — restart harness.service when systemd has given up after the
# 3-strike crash limit (StartLimitBurst).  Without this, a brief bad-code run
# can permanently halt the self-improvement loop because nothing else
# unsticks the "failed" state.
#
# Install via cron: */30 * * * * /home/ubuntu/harness-everything/deploy/heartbeat.sh
#
# Logs to syslog under tag harness-heartbeat — view with:
#   journalctl -t harness-heartbeat --since '1 day ago'

set -u

SERVICE="harness.service"
state=$(systemctl --user is-failed "$SERVICE" 2>&1 || true)

if [ "$state" = "failed" ]; then
    systemctl --user reset-failed "$SERVICE"
    if systemctl --user start "$SERVICE"; then
        logger -t harness-heartbeat "service was failed; reset + restarted OK"
    else
        logger -t harness-heartbeat "service was failed; restart attempt FAILED"
    fi
fi
