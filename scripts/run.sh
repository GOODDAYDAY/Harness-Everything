#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
    echo "Usage:"
    echo "  scripts/run.sh agent <agent_config.json>"
    echo "  scripts/run.sh pilot <pilot_config.json>"
    exit 1
fi

MODE="$1"
shift

case "$MODE" in
    agent)
        python main.py "$@"
        ;;
    pilot)
        python pilot.py "$@"
        ;;
    *)
        echo "Unknown mode: $MODE (use 'agent' or 'pilot')"
        exit 1
        ;;
esac
