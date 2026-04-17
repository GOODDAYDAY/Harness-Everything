# Harness-Everything

Provider-agnostic AI coding harness that autonomously improves codebases through iterative, multi-phase LLM loops.

Works with any LLM supporting the Anthropic API format (Claude, DeepSeek, Gemini via gateway, etc.).

## Quick Start

```bash
pip install anthropic>=0.40.0
export HARNESS_API_KEY=your-api-key

# Simple mode
python main.py "Fix the login bug" config.json

# Pipeline mode
python main.py --pipeline config/pipeline_example_self_improve.json
```

## Architecture

```
Pipeline Mode:
  Outer Round 1..N
    +-- Phase 1..M (debate or implement)
    |     +-- Inner Round 1..K
    |     |     +-- Executor (tool-use loop: read, edit, grep, bash...)
    |     |     +-- DualEvaluator (basic + diffusion, parallel)
    |     +-- Synthesis (merge best proposals)
    |     +-- Hooks (syntax check, pytest, git commit)
    +-- Auto-push (git push every N rounds)
    +-- Auto-tag (git tag + push, triggers CI/CD)
    +-- Early stop (patience-based)
```

## LLM Provider

```json
{
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/anthropic",
  "api_key": "your-key"
}
```

> DeepSeek's Anthropic-compatible endpoint is `/anthropic`, NOT `/v1`.

Verify before running a pipeline:
```bash
HARNESS_API_KEY=your-key python tests/smoke_test_deepseek.py
```

Env var fallback: `HARNESS_BASE_URL`, `HARNESS_API_KEY`.

## Pipeline Configuration

Templates in [`config/`](config/):
- [`pipeline_example_self_improve.json`](config/pipeline_example_self_improve.json) — local self-improvement
- [`pipeline_example_self_improve_server.json`](config/pipeline_example_self_improve_server.json) — unattended server (DeepSeek, 10-round chunks)
- [`pipeline_example_multi_repo.json`](config/pipeline_example_multi_repo.json) — multi-repo

### Key Fields

| Field | Default | Description |
|---|---|---|
| `outer_rounds` | 5 | Rounds per chunk |
| `inner_rounds` | 3 | Proposals per phase |
| `patience` | 3 | Early stop after N non-improving rounds |
| `max_tool_turns` | 30 | Tool calls per executor loop |
| `max_file_context_chars` | 60000 | Source code injection budget |
| `auto_push_interval` | 0 | Push every N rounds (0 = disabled) |
| `auto_tag_at_end` | false | Force tag on every pipeline exit (required for loops) |
| `auto_tag_push` | false | Push tag to remote (triggers CI) |

---

## Server Deployment (Self-Improvement Loop)

### How the Loop Works

```
Server runs N rounds --> commits --> push main --> tag --> push tag
                                                           |
GitHub Actions sees tag --> SSH to server --> smoke test --> deploy --> restart
                                                                        |
                                                   Server runs N more rounds...
```

Python imports are loaded once at startup. When the harness modifies its own code, a process restart is the only way to apply the changes. The push-tag-deploy-restart cycle makes self-improvement actually take effect.

### Server Setup (One-Time)

```bash
# 1. Clone
git clone https://github.com/GOODDAYDAY/Harness-Everything.git ~/harness-everything
cd ~/harness-everything

# 2. Venv OUTSIDE workspace (security: harness tools can't access it)
python3.11 -m venv ~/harness-venv
~/harness-venv/bin/pip install 'anthropic>=0.40.0' pytest

# 3. Git identity
git config user.name 'GOODDAYDAY'
git config user.email '865700600@qq.com'

# 4. Server config (auto-synced by CI from config/pipeline_example_self_improve_server.json)
cp config/pipeline_example_self_improve_server.json pipeline_server.json

# 5. API key
mkdir -p ~/.config/harness
echo "HARNESS_API_KEY=your-deepseek-key" > ~/.config/harness/env
chmod 600 ~/.config/harness/env

# 6. Systemd service
mkdir -p ~/.config/systemd/user logs
cp deploy/harness.service ~/.config/systemd/user/
loginctl enable-linger $(whoami)
systemctl --user daemon-reload
systemctl --user enable harness.service

# 7. Cron jobs (heartbeat + cleanup)
(crontab -l 2>/dev/null; \
 echo "*/30 * * * * $HOME/harness-everything/deploy/heartbeat.sh"; \
 echo "0 4 * * * $HOME/harness-everything/deploy/cleanup_runs.sh") | crontab -

# 8. SSH keys for GitHub
# Push key (server -> GitHub):
ssh-keygen -t ed25519 -N '' -f ~/.ssh/github_push -C 'harness-push'
cat >> ~/.ssh/config <<'EOF'
Host github.com-harness
    HostName github.com
    User git
    IdentityFile ~/.ssh/github_push
    IdentitiesOnly yes
EOF
git remote set-url origin git@github.com-harness:GOODDAYDAY/Harness-Everything.git
# Add public key to GitHub repo -> Settings -> Deploy Keys (with write access)

# Deploy key (GitHub Actions -> server):
ssh-keygen -t ed25519 -N '' -f ~/.ssh/harness_deploy -C 'harness-deploy'
cat ~/.ssh/harness_deploy.pub >> ~/.ssh/authorized_keys
# Add PRIVATE key to GitHub repo -> Settings -> Secrets -> DEPLOY_SSH_KEY
```

### Trigger the First Chunk

```bash
git tag harness-r-0 -m "bootstrap"
git push origin harness-r-0
```

GitHub Actions deploys and starts the service. After 10 rounds, harness pushes a new tag. CI redeploys. Loop begins.

---

## Operations Playbook

### Monitor

```bash
# Live logs
ssh server "tail -f ~/harness-everything/logs/harness.log"

# Recent commits (pushed every round)
git log --oneline -20

# GitHub Actions runs
gh run list -L 10

# Heartbeat/cleanup cron logs
ssh server "journalctl -t harness-heartbeat -t harness-cleanup --since '1 day ago'"
```

### Push a Fix While Loop is Running

Just push to main. The harness does `git pull --rebase` before every push, so your commits are integrated automatically.

```bash
git commit -am "fix: something" && git push origin main
```

- No file conflicts: harness rebases its commits on top of yours, pushes cleanly.
- Conflicting files: rebase aborts, harness skips push this round, retries next round.
- Code changes take full effect at next chunk restart.

### Change Pipeline Config

Edit the tracked template in `config/`, push. CI auto-copies it to `pipeline_server.json` on every deploy.

```bash
vim config/pipeline_example_self_improve_server.json
git commit -am "config: adjust patience" && git push
# Takes effect at next chunk restart
```

> Never edit `pipeline_server.json` on the server directly — CI overwrites it.

### Stop After Current Chunk (Graceful)

```bash
ssh server "touch ~/.config/harness/STOP_AFTER_CHUNK"
```

Current chunk finishes normally. CI sees the marker, does NOT restart. Marker auto-deleted.

### Resume the Loop

```bash
# Direct start
ssh server "systemctl --user start harness.service"

# Or via CI
git tag harness-r-resume -m "resume" && git push origin harness-r-resume
```

### Emergency Stop

```bash
ssh server "systemctl --user stop harness.service"
```

Commits from completed rounds are already pushed. Only in-progress round work is lost.

### Full Shutdown (Prevent All Restarts)

```bash
ssh server "
  systemctl --user stop harness.service
  systemctl --user disable harness.service
  crontab -l | grep -v harness-everything | crontab -
"
```

### Rewrite Git History

```bash
# Stop service first (running harness can't push to rewritten history)
ssh server "systemctl --user stop harness.service"

# Example: fix author
echo "NewName <new@email> OldName <old@email>" > /tmp/mailmap.txt
git-filter-repo --refs main --mailmap /tmp/mailmap.txt --force
git push --force origin main

# Reset server
ssh server "cd ~/harness-everything && git fetch --all --prune --force && git reset --hard origin/main"
```

---

## Safety Nets

| Threat | Protection |
|---|---|
| Patience early stop / low score -> no tag -> loop dies | `auto_tag_at_end: true` forces tag at every exit |
| Harness modifies deploy infrastructure | Phase prompts include `SELF-IMPROVEMENT LOOP PROTECTION` blocklist |
| Service crashes 3x in 10min -> systemd gives up | Heartbeat cron resets-failed and restarts every 30min |
| Disk fills with old runs | Cleanup cron deletes `run_*` dirs older than 7 days |
| User pushes to main mid-chunk -> non-fast-forward | `git pull --rebase` before every push |
| Bad code deployed | CI smoke test (py_compile + import check); rollback to `harness-last-good` on failure |

## Project Structure

```
main.py                                    # CLI entry point
config/
  pipeline_example_self_improve.json       # Local template
  pipeline_example_self_improve_server.json  # Server template (auto-synced by CI)
  pipeline_example_multi_repo.json         # Multi-repo template
harness/
  __init__.py
  core/
    config.py          # HarnessConfig, PipelineConfig
    llm.py             # Async LLM client (retry, pruning, file-read cache)
    security.py        # Path security: homoglyphs, null bytes, control chars
    artifacts.py       # Hierarchical run/round/phase/inner storage
    checkpoint.py      # Resume-safe .done markers
    project_context.py # Project metadata injection
  pipeline/
    pipeline_loop.py   # Outer rounds: auto-push, auto-tag, meta-review, shutdown
    phase_runner.py    # Inner rounds, synthesis, parallel debate
    phase.py           # PhaseConfig, InnerResult, PhaseResult (data only)
    simple_loop.py     # Simple mode orchestrator
    executor.py        # Tool-use agentic loop
    planner.py         # Three-way plan generation
    hooks.py           # SyntaxCheck, Pytest, GitCommitHook
    memory.py          # Cross-round JSONL learning
    metrics.py         # Structured per-phase metrics
    three_way.py       # Conservative/aggressive/merge resolver
  evaluation/
    dual_evaluator.py  # Basic + Diffusion parallel evaluation
    evaluator.py       # Three-way evaluator
    static_analysis.py # Deterministic code checks
  tools/               # 30+ tools (file ops, search, git, bash, AST analysis)
  prompts/             # System prompts for planner, evaluator, synthesis
deploy/
  harness.service      # systemd user unit
  heartbeat.sh         # Cron: restart after 3-strike failure
  cleanup_runs.sh      # Cron: delete old run_* dirs
.github/workflows/
  deploy.yml           # Tag-triggered: smoke test + deploy + rollback
tests/
  smoke_test_deepseek.py  # DeepSeek tool-loop verifier
  test_*.py               # Unit tests
```

## Requirements

- Python 3.11+
- `anthropic>=0.40.0`

## License

MIT
