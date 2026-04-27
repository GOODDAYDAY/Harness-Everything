# Harness-Everything

Provider-agnostic AI coding harness that autonomously improves codebases. A single LLM with full tool access runs connected tool-use cycles, committing improvements and verifying them with syntax/import/static checks.

Works with any LLM supporting the Anthropic API format (Claude, DeepSeek, Gemini via gateway, etc.).

## Quick Start

```bash
pip install anthropic>=0.40.0
export HARNESS_API_KEY=your-api-key

python main.py config/agent_example.json
```

## Architecture

```
AgentLoop.run()
│
├── for cycle in range(max_cycles):
│   │
│   ├── Build system prompt = mission + persistent notes (agent_notes.md)
│   ├── call_with_tools() — up to max_tool_turns tool calls
│   │     LLM reads/searches/edits files via 40+ tools
│   │
│   ├── Post-cycle hooks:
│   │     ├── SyntaxCheckHook (py_compile, gates commit)
│   │     ├── ImportSmokeHook (subprocess import, gates commit)
│   │     └── StaticCheckHook (ruff/pyflakes, gates commit)
│   │
│   ├── If hooks pass + auto_commit: git add -A && git commit
│   ├── Auto-evaluation via DualEvaluator (basic + diffusion scoring)
│   ├── Periodic checkpoint (meta-review + squash + tag, parallel LLM)
│   └── Check for MISSION COMPLETE / MISSION BLOCKED signals
│
└── Auto-push (git pull --rebase + push)
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

Env var fallback: `HARNESS_BASE_URL`, `HARNESS_API_KEY`.

## Configuration

Templates in [`config/`](config/):
- [`agent_example.json`](config/agent_example.json) — minimal example
- [`agent_self_improve.json`](config/agent_self_improve.json) — local self-improvement
- [`agent_example_self_improve_server.json`](config/agent_example_self_improve_server.json) — unattended server deployment

### Key Fields

| Field | Default | Description |
|---|---|---|
| `model` | `"bedrock/claude-sonnet-4-6"` | LiteLLM-prefixed model ID |
| `max_tool_turns` | 60 | Tool calls per cycle |
| `auto_commit` | false | Commit after each passing cycle |
| `meta_review_interval` | 5 | Checkpoint every N cycles (review + squash + tag) |
| `auto_squash` | false | LLM groups and squashes commits at checkpoint |
| `auto_tag` | false | Tag HEAD at each checkpoint |
| `import_smoke_modules` | `[]` | Modules to verify in subprocess |
| `mission` | `""` | Task description for the agent |

---

## Server Deployment (Self-Improvement Loop)

### How the Loop Works

```
Server runs N cycles --> commits --> push main --> tag --> push tag
                                                           |
GitHub Actions sees tag --> SSH to server --> smoke test --> deploy --> restart
                                                                        |
                                                   Server runs N more cycles...
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

# 4. API key
mkdir -p ~/.config/harness
echo "HARNESS_API_KEY=your-deepseek-key" > ~/.config/harness/env
chmod 600 ~/.config/harness/env

# 5. Systemd service
mkdir -p ~/.config/systemd/user logs
cp deploy/harness.service ~/.config/systemd/user/
loginctl enable-linger $(whoami)
systemctl --user daemon-reload
systemctl --user enable harness.service

# 6. Cron jobs (heartbeat + cleanup)
(crontab -l 2>/dev/null; \
 echo "*/30 * * * * $HOME/harness-everything/deploy/heartbeat.sh"; \
 echo "0 4 * * * $HOME/harness-everything/deploy/cleanup_runs.sh") | crontab -

# 7. SSH keys for GitHub (see config/agent_example_self_improve_server.json for details)
```

### Operations

| Action | Command |
|---|---|
| Live logs | `ssh server "tail -f ~/harness-everything/logs/harness.log"` |
| Recent commits | `git log --oneline -20` |
| Push a fix (no downtime) | `git push origin main` — agent rebases automatically |
| Graceful stop after current run | `ssh server "touch ~/.config/harness/STOP_AFTER_CHUNK"` |
| Resume | `systemctl --user start harness.service` |
| Emergency stop | `systemctl --user stop harness.service` |

---

## Project Structure

```
main.py                              # CLI entry point
config/
  agent_example.json                 # Minimal example
  agent_self_improve.json            # Local self-improvement
  agent_example_self_improve_server.json  # Server deployment
harness/
  core/
    config.py            # HarnessConfig
    llm.py               # Async LLM client (retry, pruning, streaming)
    hooks.py             # Verification hooks (syntax, import smoke, static)
    artifacts.py         # Hierarchical run/cycle storage
    checkpoint.py        # Resume-safe .done markers
    project_context.py   # Project metadata injection
    security.py          # Path security: homoglyphs, null bytes, traversal
    signal_util.py       # Shutdown signal handlers
  agent/
    agent_loop.py        # AgentLoop — the main execution engine
    cycle_metrics.py     # Per-cycle metrics collection
  evaluation/
    dual_evaluator.py    # Dual isolated evaluator (basic + diffusion)
    static_analysis.py   # Deterministic code checks
    metrics.py           # Structured metrics collector
  prompts/
    dual_evaluator.py    # Evaluator prompt templates
    agent_meta_review.py # Meta-review prompt
  tools/
    base.py              # Tool ABC, ToolResult, path security
    registry.py          # ToolRegistry (dispatch, alias normalization)
    *.py                 # 40+ tools (file ops, search, git, bash, AST, etc.)
deploy/
  harness.service        # systemd user unit
  heartbeat.sh           # Cron: restart after failure
  cleanup_runs.sh        # Cron: delete old run_* dirs
.github/workflows/
  deploy.yml             # Tag-triggered: smoke test + deploy + rollback
```

## Safety Nets

| Threat | Protection |
|---|---|
| LLM writes broken code | SyntaxCheckHook + ImportSmokeHook + StaticCheckHook gate commits |
| LLM escapes workspace | `_check_path()` in every file tool; null byte / homoglyph / symlink checks |
| Service crashes repeatedly | Heartbeat cron resets-failed and restarts every 30min |
| Disk fills with old runs | Cleanup cron deletes `run_*` dirs older than 7 days |
| User pushes mid-run | `git pull --rebase` before every push |
| Bad code deployed to server | CI smoke test (py_compile + import check); rollback to `harness-last-good` |

## Requirements

- Python 3.11+
- `anthropic>=0.40.0`

## License

MIT
