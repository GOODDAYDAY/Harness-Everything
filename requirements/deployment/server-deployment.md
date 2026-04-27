# DEP-01: Server Deployment

Status: **Active**
Source: `deploy/harness.service`, `main.py`

---

## systemd Service Unit

File: `deploy/harness.service`

### Unit section

| Field | Value |
|-------|-------|
| Description | `Harness self-improvement loop` |
| Documentation | `https://github.com/GOODDAYDAY/Harness-Everything` |
| After | `network-online.target` |
| Wants | `network-online.target` |

The service waits for network to be online before starting (required for LLM API calls).

### Service section

| Field | Value | Notes |
|-------|-------|-------|
| Type | `simple` | |
| WorkingDirectory | `/home/ubuntu/harness-everything` | |
| EnvironmentFile | `/home/ubuntu/.config/harness/env` | Must contain `HARNESS_API_KEY=...` |
| ExecStart | `/home/ubuntu/harness-venv/bin/python main.py --agent /home/ubuntu/harness-everything/agent_server.json` | `--agent` is a legacy flag, stripped by main.py |

### Restart policy

| Field | Value | Notes |
|-------|-------|-------|
| Restart | `on-failure` | Clean exit (code 0) stops the service; crash exits trigger retry |
| RestartSec | `60s` | Wait 60 seconds between retries |
| StartLimitBurst | `3` | Maximum 3 restart attempts |
| StartLimitIntervalSec | `600` | Within a 10-minute window |

Behaviour: after 3 failed restarts within 600 seconds, systemd gives up. The heartbeat cron (`deploy/heartbeat.sh`) detects this "failed" state and resets it.

### Logging

| Field | Value |
|-------|-------|
| StandardOutput | `append:/home/ubuntu/harness-everything/logs/harness.log` |
| StandardError | `append:/home/ubuntu/harness-everything/logs/harness.log` |

Both stdout and stderr go to the same rotating log file, avoiding journald storage.

### Timeouts

| Field | Value | Notes |
|-------|-------|-------|
| TimeoutStartSec | `infinity` | No startup timeout (agent runs may be very long) |
| TimeoutStopSec | `120` | 2-minute grace period for shutdown |

### Install section

| Field | Value |
|-------|-------|
| WantedBy | `default.target` |

This is a **user-level** service (uses `systemctl --user`, as seen in heartbeat.sh).

---

## Entry Point (main.py)

File: `main.py` (78 lines)

### Usage

```
python main.py <config.json>
python main.py --agent <config.json>   # legacy form, still supported
```

### Behaviour

1. Calls `setup_logging()` -- configures Python `logging.basicConfig` with format `%(asctime)s [%(levelname)s] %(name)s: %(message)s`, datefmt `%H:%M:%S`, default level `INFO`
2. Strips legacy `--agent` flag from args if present
3. Reads config JSON from the first positional argument
4. Parses config via `AgentConfig.from_dict()`
5. Runs the agent loop via `asyncio.run(run_agent(agent_cfg))`
6. Prints summary to stdout: mission_status, cycles_run, total_tool_calls, run_dir, first 500 chars of final summary

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Mission complete OR at least one cycle produced work |
| 1 | No config file argument provided (usage error) |
| 2 | Zero-work catastrophe: agent completed cycles but made zero tool calls |

Exit code 2 is treated as a failure by systemd's `Restart=on-failure`, triggering retry. This also feeds into heartbeat.sh's zombie detection for older deployments.

---

## Environment Setup

The `EnvironmentFile` at `/home/ubuntu/.config/harness/env` must provide:
- `HARNESS_API_KEY` -- API key for the LLM provider

The `api_key` field in agent config JSONs is left empty (`""`); the runtime resolves it from the `HARNESS_API_KEY` environment variable (fallback chain defined in `harness/core/llm.py`: `config.api_key` → `HARNESS_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY`).
