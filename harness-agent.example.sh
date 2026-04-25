#!/usr/bin/env bash
# harness-agent.example.sh — manage a Harness V5 autonomous agent
#
# V5 adds: multi-axis evaluation, structured experience memory, exploration
# phases, meta-agent strategy layer, and hot-reloadable self-configuration.
# The agent can now modify its own evaluator prompts and weights at runtime.
#
# Copy this file, rename it, and adjust the configuration section below
# to match your project. Then:
#
#   ./harness-agent.sh start   — launch the agent, tail logs
#   ./harness-agent.sh stop    — gracefully stop (finishes current cycle)
#   ./harness-agent.sh pause   — pause after current cycle
#   ./harness-agent.sh resume  — resume a paused agent
#   ./harness-agent.sh status  — show running state + last 10 log lines
#   ./harness-agent.sh logs    — tail the log file (Ctrl-C to detach)
#
# The agent runs in the background. Logs go to LOG_FILE. The process
# PID is stored in PID_FILE so stop/status can find it.
#
# Pause mechanism: `pause` creates a marker file (.harness.pause) in the
# workspace. The agent checks for this file between cycles — when present,
# it sleeps until the file is removed (via `resume`). This lets a cycle
# finish cleanly before pausing, unlike SIGINT which interrupts mid-cycle.

set -euo pipefail

# ── Configuration — EDIT THESE ────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Path to the Harness-Everything checkout
HARNESS_DIR="$SCRIPT_DIR"

# Python virtual environment inside the Harness-Everything directory
VENV="$HARNESS_DIR/.venv"
PYTHON="$VENV/bin/python"

# Agent or pipeline config JSON:
#   Agent mode:  python main.py --agent config/agent_example.json
#   Pipeline:    python main.py --pipeline config/pipeline_example_self_improve.json
# See config/pipeline_example_self_improve.json for V5 features:
#   - evaluation_engine: "multi_axis" (5-dim vector scoring)
#   - exploration_interval (novelty-weighted exploration phases)
#   - meta_agent_interval (strategy layer running every N rounds)
AGENT_CONFIG="$HARNESS_DIR/config/agent_self_improve.json"

# Runtime files (adjust names per project to avoid collisions)
PID_FILE="$SCRIPT_DIR/.harness-agent.pid"
LOG_FILE="$SCRIPT_DIR/.harness-agent.log"
PAUSE_FILE="$HARNESS_DIR/.harness.pause"

# V5 hot-reloadable config (editable by meta-agent at runtime):
#   harness/prompts/eval_basic.txt     — basic evaluator prompt
#   harness/prompts/eval_diffusion.txt — diffusion evaluator prompt
#   harness/config/eval_weights.json   — multi-axis weights

# ── Helpers ───────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

ensure_env() {
    # Verify the Python venv exists
    [[ -x "$PYTHON" ]] || die "Python venv not found: $PYTHON
  Set up with:  cd $HARNESS_DIR && python3 -m venv .venv && .venv/bin/pip install -e ."

    # Verify the agent config exists
    [[ -f "$AGENT_CONFIG" ]] || die "Agent config not found: $AGENT_CONFIG"

    # Verify API key is set (via env var or config)
    # Option 1: export HARNESS_API_KEY in your shell profile
    # Option 2: set api_key in the agent config JSON
    # Uncomment and adapt one of these if you need auto-loading:
    #
    # --- Load from a .env file ---
    # if [[ -f "$HARNESS_DIR/.env" ]]; then
    #     set -a; source "$HARNESS_DIR/.env"; set +a
    # fi
    #
    # --- Load from a secrets manager ---
    # export HARNESS_API_KEY="$(some-secrets-tool get harness-api-key)"

    if [[ -z "${HARNESS_API_KEY:-}" ]]; then
        # Check if api_key is set in the config itself
        local config_key
        config_key=$(python3 -c "
import json, sys
c = json.load(open('$AGENT_CONFIG'))
print(c.get('harness', {}).get('api_key', ''))
" 2>/dev/null || true)
        if [[ -z "$config_key" ]]; then
            die "No API key found. Set HARNESS_API_KEY env var or api_key in the agent config."
        fi
    fi
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_start() {
    is_running && die "Agent already running (PID $(cat "$PID_FILE")). Run '$0 stop' first."

    ensure_env

    echo ""
    echo "Starting Harness agent"
    echo "  config : $AGENT_CONFIG"
    echo "  log    : $LOG_FILE"
    echo ""

    nohup "$PYTHON" "$HARNESS_DIR/main.py" --agent "$AGENT_CONFIG" \
        >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    # Wait a moment and check for immediate crash
    sleep 1
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "Process exited immediately. Last 20 lines of log:"
        tail -20 "$LOG_FILE"
        exit 1
    fi

    echo "Agent started (PID $(cat "$PID_FILE"))"
    echo ""
    echo "Tailing logs (Ctrl-C to detach — agent keeps running):"
    echo "--------------------------------------------------------------"
    tail -f "$LOG_FILE"
}

cmd_stop() {
    if ! is_running; then
        echo "Agent is not running."
        rm -f "$PID_FILE"
        return
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo "Stopping agent (PID $pid)..."

    # Phase 1: SIGINT — agent finishes current cycle cleanly
    kill -INT "$pid" 2>/dev/null || true
    echo "Sent SIGINT — waiting for agent to finish current cycle..."
    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        (( waited++ ))
        # Progress indicator every 5s
        if (( waited % 5 == 0 )); then
            printf "  … still waiting (%ds)\n" "$waited"
        fi
        # After 120s on SIGINT, escalate to SIGTERM
        if (( waited == 120 )); then
            echo "Still running after 120s — sending SIGTERM..."
            kill -TERM "$pid" 2>/dev/null || true
        fi
        # After 150s total, force kill
        if (( waited == 150 )); then
            echo "Still alive after 150s — force killing (SIGKILL)..."
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
            break
        fi
    done

    rm -f "$PID_FILE"
    echo "Agent stopped. (took ${waited}s)"
}

cmd_pause() {
    if ! is_running; then
        echo "Agent is not running — nothing to pause."
        return
    fi
    if [[ -f "$PAUSE_FILE" ]]; then
        echo "Already paused (pause file exists: $PAUSE_FILE)"
        return
    fi
    touch "$PAUSE_FILE"
    echo "Pause requested. Agent will pause after the current cycle finishes."
    echo "  pause file: $PAUSE_FILE"
    echo "  Run '$0 resume' to continue."
}

cmd_resume() {
    if [[ ! -f "$PAUSE_FILE" ]]; then
        echo "Not paused (no pause file: $PAUSE_FILE)"
        return
    fi
    rm -f "$PAUSE_FILE"
    echo "Pause file removed. Agent will resume on next poll (~30s)."
}

cmd_status() {
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        if [[ -f "$PAUSE_FILE" ]]; then
            echo "PAUSED (PID $pid)"
            echo "  Run '$0 resume' to continue."
        else
            echo "RUNNING (PID $pid)"
        fi
        echo "  config : $AGENT_CONFIG"
        echo "  log    : $LOG_FILE"
        echo ""
        echo "Last 10 lines:"
        echo "--------------------------------------------------------------"
        tail -10 "$LOG_FILE" 2>/dev/null || echo "  (no log yet)"
    else
        echo "NOT RUNNING"
        [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    fi
}

cmd_logs() {
    [[ -f "$LOG_FILE" ]] || die "No log file yet: $LOG_FILE"
    echo "Tailing $LOG_FILE (Ctrl-C to detach):"
    echo "--------------------------------------------------------------"
    tail -f "$LOG_FILE"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "${1:-}" in
    start)  cmd_start  ;;
    stop)   cmd_stop   ;;
    pause)  cmd_pause  ;;
    resume) cmd_resume ;;
    status) cmd_status ;;
    logs)   cmd_logs   ;;
    *)
        echo "Usage: $(basename "$0") {start|stop|pause|resume|status|logs}"
        echo ""
        echo "  start   — launch the agent and tail logs"
        echo "  stop    — gracefully stop (finishes current cycle)"
        echo "  pause   — pause after current cycle finishes"
        echo "  resume  — resume a paused agent"
        echo "  status  — show running state + last log lines"
        echo "  logs    — tail the log file (Ctrl-C to detach)"
        exit 1
        ;;
esac
