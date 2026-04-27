# Deployment Domain

Status: **Active**

Server deployment, health monitoring, run cleanup, and agent configuration for the Harness-Everything autonomous loop.

## Scenarios

| ID | Document | Summary |
|----|----------|---------|
| DEP-01 | [server-deployment.md](server-deployment.md) | systemd user service, environment setup, logging |
| DEP-02 | [health-monitoring.md](health-monitoring.md) | Heartbeat cron script for crash recovery and zombie detection |
| DEP-03 | [run-cleanup.md](run-cleanup.md) | Cron script to delete old run directories |
| DEP-04 | [agent-configuration.md](agent-configuration.md) | Config file inventory and schema |
| DEP-05 | [self-update-loop.md](self-update-loop.md) | CI/CD deploy workflow (tag-triggered smoke test + deploy + rollback) |

## Source files

- `deploy/harness.service` -- systemd unit
- `deploy/heartbeat.sh` -- crash recovery cron
- `deploy/cleanup_runs.sh` -- disk cleanup cron
- `config/agent_example.json` -- minimal agent config template
- `config/agent_example_self_improve_server.json` -- server deploy config template
- `config/agent_self_improve.json` -- local self-improve config
- `config/agent_example_project.json` -- ExampleProject agent config
- `main.py` -- CLI entry point

## Key paths on the deploy server

| Path | Purpose |
|------|---------|
| `/home/ubuntu/harness-everything` | Working directory |
| `/home/ubuntu/harness-venv/bin/python` | Python venv |
| `/home/ubuntu/.config/harness/env` | Environment file (contains `HARNESS_API_KEY`) |
| `/home/ubuntu/harness-everything/logs/harness.log` | Rotating log file |
| `/home/ubuntu/harness-everything/harness_output` | Run output directories |
| `/home/ubuntu/harness-everything/agent_server.json` | Active server config (copied from template by deploy workflow) |
