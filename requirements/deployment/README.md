# Deployment Domain

The deployment domain covers everything that keeps the Harness self-improvement loop running continuously on the server: service lifecycle management, crash recovery, health monitoring, disk housekeeping, and the CI/CD pipeline that deploys new code and restarts the service.

The autonomous agent runs in fixed-size chunks of work cycles. At the end of each chunk it commits, pushes, and tags. The pushed tag triggers a CI pipeline that deploys the new code to the server and restarts the service, forming a closed self-update loop.

## Scenarios

| ID | Scenario | Document | Scope |
|----|----------|----------|-------|
| S-01 | Service lifecycle | [server-operations.md](server-operations.md) | Systemd unit, startup, clean exit, crash retry |
| S-02 | Crash recovery | [server-operations.md](server-operations.md) | Three-strike limit, heartbeat cron, zero-work detection |
| S-03 | Health monitoring | [server-operations.md](server-operations.md) | Heartbeat script, zombie detection, logging |
| S-04 | Disk cleanup | [server-operations.md](server-operations.md) | Retention policy, scheduled purge of old run output |
| S-05 | Self-update loop | [ci-cd.md](ci-cd.md) | Tag-triggered deploy, smoke test, restart |
| S-06 | Rollback | [ci-cd.md](ci-cd.md) | Last-known-good tag, automatic rollback on smoke failure |
| S-07 | Config management | [ci-cd.md](ci-cd.md) | Template propagation from tracked example to server config |
| S-08 | Pause and resume | [ci-cd.md](ci-cd.md) | Pause file (intra-chunk), stop marker (inter-chunk) |
| S-09 | Auto-tagging | [ci-cd.md](ci-cd.md) | Periodic tagging at checkpoint, tag push triggers deploy |
| S-10 | Shell command safety | [ci-cd.md](ci-cd.md) | Denylist enforcement, known gaps |
