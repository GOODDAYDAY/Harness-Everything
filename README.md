# Harness-Everything

Provider-agnostic AI coding harness that autonomously improves codebases through iterative, multi-phase LLM loops. **V5** adds multi-axis evaluation, structured experience memory, exploration mode, and a MetaAgent strategy layer that can modify its own evaluator prompts and weights at runtime.

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
Pipeline Mode (V5):
  Outer Round 1..N
    ├── MetaAgent (every N rounds: analyze trends, adjust strategy)
    ├── Exploration Phase (every N rounds: novelty-weighted attempts)
    +-- Phase 1..M (debate, implement, or exploration)
    |     +-- Inner Round 1..K
    |     |     +-- Executor (tool-use loop: read, edit, grep, bash...)
    |     |     +-- MultiAxisEvaluator (5-dim vector: correctness, code_quality,
    |     |           arch_health, novelty, alignment — weights hot-reloaded)
    |     +-- Synthesis (merge best proposals)
    |     +-- ExperienceStore (record + reflect + abstract patterns)
    |     +-- Hooks (syntax check, pytest, git commit)
    +-- Auto-push (git pull --rebase + push every N rounds)
    +-- Auto-tag (git tag + push, triggers CI/CD)
    +-- Early stop (patience-based)
```

## V5 Features

| Module | File | Description |
|---|---|---|
| **Multi-Axis Eval** | `harness/evaluation/multi_axis.py` | 5-dim vector (correctness, code_quality, arch_health, novelty, alignment) replaces single 0–10 score. Weights hot-reloaded from `harness/config/eval_weights.json`. |
| **Experience Memory** | `harness/core/experience.py` | Structured memory replacing JSONL logs. Each entry records the eval vector, LLM self-reflection, abstracted patterns, and searchable tags. |
| **Exploration Mode** | `harness/pipeline/phase_runner.py` | Novelty-weighted exploration rounds (novelty at 50%) injected every N rounds. Relaxed gating lets the agent try bold new approaches. |
| **MetaAgent** | `harness/pipeline/meta_agent.py` | Strategy layer running every N rounds. Reads experience store + score trends, outputs: focus axis, weight adjustments, exploration frequency. |
| **Hot-Reload Config** | `harness/core/eval_config.py` | Evaluator prompts and weights stored as plain `.txt`/`.json` files on disk. Read fresh every evaluation — changes take effect without restart. |
| **Self-Awareness** | `harness/tools/self_config.py` | `GetSelfConfigTool` lets the agent discover where its own config files live and modify them at runtime. |

See [`docs/architecture-zh.md`](docs/architecture-zh.md) (Chinese) or [`docs/architecture-en.md`](docs/architecture-en.md) (English) for the full evolution from V1 (raw while-loop) through V5 (self-modifying agent).

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
| `evaluation_engine` | `"dual"` | V5: `"dual"` (legacy) or `"multi_axis"` (5-dim vector) |
| `exploration_interval` | 0 | V5: Novelty-weighted exploration every N rounds (0=off) |
| `meta_agent_interval` | 0 | V5: Strategy-layer meta-agent every N rounds (0=off) |
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
    eval_config.py     # **V5** Hot-reload evaluator prompts & weights from disk
    experience.py      # **V5** Structured memory with reflection & abstraction
    security.py        # Path security: homoglyphs, null bytes, control chars
    artifacts.py       # Hierarchical run/round/phase/inner storage
    checkpoint.py      # Resume-safe .done markers
    project_context.py # Project metadata injection
  pipeline/
    pipeline_loop.py   # Outer rounds: auto-push, auto-tag, meta-review, shutdown
    meta_agent.py      # **V5** Strategy layer: analyzes trends, adjusts direction
    phase_runner.py    # Inner rounds, synthesis, parallel debate
    phase.py           # PhaseConfig, InnerResult, PhaseResult (data only)
    simple_loop.py     # Simple mode orchestrator
    executor.py        # Tool-use agentic loop
    planner.py         # Three-way plan generation
    hooks.py           # SyntaxCheck, Pytest, GitCommitHook
    memory.py          # Cross-round JSONL learning (V4)
    metrics.py         # Structured per-phase metrics
    three_way.py       # Conservative/aggressive/merge resolver
  evaluation/
    multi_axis.py      # **V5** 5-dim vector evaluator (replaces single score)
    dual_evaluator.py  # Basic + Diffusion parallel evaluation (V4)
    evaluator.py       # Three-way evaluator
    static_analysis.py # Deterministic code checks
  tools/
    self_config.py      # **V5** Agent self-awareness: discover own config paths
    *.py                # 30+ tools (file ops, search, git, bash, AST analysis)
  prompts/
    eval_basic.txt      # **V5** Basic evaluator prompt (hot-reloadable)
    eval_diffusion.txt  # **V5** Diffusion evaluator prompt (hot-reloadable)
    *.py                # System prompts for planner, evaluator, synthesis (V4)
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
