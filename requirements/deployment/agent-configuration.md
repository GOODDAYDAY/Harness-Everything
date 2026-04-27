# DEP-04: Agent Configuration

Status: **Active**
Source: `config/agent_example.json`, `config/agent_example_self_improve_server.json`, `config/agent_self_improve.json`, `config/agent_example_project.json`

---

## Config File Inventory

| File | Purpose | Model | Workspace | Continuous |
|------|---------|-------|-----------|------------|
| `config/agent_example.json` | Minimal example template | `bedrock/claude-sonnet-4-6` | `.` (relative) | No (`max_cycles: 50`) |
| `config/agent_example_self_improve_server.json` | Server deploy template (copied to `agent_server.json` by deploy workflow) | `deepseek-chat` | `/home/ubuntu/harness-everything` | No (`max_cycles: 20`) |
| `config/agent_self_improve.json` | Local self-improvement (macOS) | `bedrock/claude-sonnet-4-6` | `/home/user/harness/Harness-Everything` | Yes (`max_cycles: 999`) |
| `config/agent_example_project.json` | ExampleProject project agent (macOS) | `vertex/claude-sonnet-4-6` | `/home/user/harness/ExampleProject` | Yes (`max_cycles: 999`) |

---

## Config Schema

Agent config files are JSON objects with the following top-level structure. Fields prefixed with `//` are documentation comments (ignored by the parser).

### `harness` section (LLM and runtime settings)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `model` | string | Yes | -- | LiteLLM model identifier (e.g. `bedrock/claude-sonnet-4-6`, `deepseek-chat`, `vertex/claude-sonnet-4-6`) |
| `max_tokens` | int | Yes | -- | Maximum output tokens per LLM call. Values seen: `16384` (Sonnet-class), `8000` (DeepSeek) |
| `base_url` | string | No | `""` | LLM API endpoint. Empty string = use default for provider. Examples: `""`, `https://api.deepseek.com/anthropic`, `http://127.0.0.1:9099` |
| `api_key` | string | No | `""` | API key. Left empty in tracked configs; resolved from `HARNESS_API_KEY` env var at runtime |
| `workspace` | string | Yes | -- | Root directory the agent operates in |
| `allowed_paths` | string[] | Yes | -- | Filesystem paths the agent is allowed to access. Usually matches `workspace` |
| `allowed_tools` | string[] | No | `[]` | Explicit tool allowlist. Empty = all tools available |
| `extra_tools` | string[] | No | `[]` | Additional tools to register beyond defaults |
| `bash_command_denylist` | string[] | No | -- | Commands blocked from bash tool execution |
| `max_tool_turns` | int | No | -- | Maximum tool call turns per cycle. Values seen: `150`, `200` |
| `max_concurrent_llm_calls` | int | No | -- | Parallel LLM call limit. Value seen: `4` |
| `log_level` | string | No | `"INFO"` | Python logging level |

### Top-level fields (agent behaviour)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `mission` | string | Yes | -- | Full mission prompt injected into the agent's system context. Defines what the agent should do |
| `max_cycles` | int | Yes | -- | Maximum number of agent cycles before exit. `999` for continuous, `20` or `50` for chunked runs |
| `max_notes_cycles` | int | No | -- | Number of recent cycles' notes to retain. Values seen: `20`, `30` |
| `continuous` | bool | No | `false` | When `true`, agent runs indefinitely (up to `max_cycles`). Absent or `false` = chunked mode |
| `cycle_hooks` | string[] | No | -- | Hooks to run after each cycle. Values seen: `["syntax", "static", "import_smoke"]`, `["syntax", "static"]` |
| `import_smoke_modules` | string[] | No | -- | Python modules to import-test during `import_smoke` hook |
| `import_smoke_calls` | string[] | No | `[]` | Callable expressions to test during import smoke (always empty in current configs) |
| `syntax_check_patterns` | string[] | No | -- | Glob patterns for files to syntax-check. Examples: `"harness/**/*.py"`, `"tests/**/*.py"`, `"main.py"`, `"bridge/**/*.py"` |
| `auto_commit` | bool | No | `false` | Automatically commit changes after each cycle |
| `commit_repos` | string[] | No | -- | Repository paths to commit. Value: `["."]` |
| `auto_push` | bool | No | `false` | Automatically push after committing |
| `auto_push_remote` | string | No | -- | Git remote for auto-push. Value: `"origin"` |
| `auto_push_branch` | string | No | -- | Git branch for auto-push. Value: `"main"` |
| `auto_evaluate` | bool | No | `false` | Run dual evaluation after each cycle. Only explicitly set in `agent_example_project.json` |
| `meta_review_interval` | int | No | -- | Run meta-review every N cycles. Value seen: `5` (in `agent_example_project.json`) |
| `auto_tag_interval` | int | No | -- | Create a git tag every N cycles. Value seen: `20` |
| `auto_tag_prefix` | string | No | -- | Prefix for auto-generated tags. Value: `"harness-r"` |
| `auto_tag_push` | bool | No | `false` | Push tags to remote after creation |
| `output_dir` | string | No | -- | Directory for run output. Examples: `"harness_output"`, `/home/ubuntu/harness-everything/harness_output`, `/home/user/harness/harness_output` |
| `run_id` | string/null | No | `null` | Explicit run ID. `null` = auto-generated |

---

## Per-Config Details

### agent_example.json (minimal template)

- Model: `bedrock/claude-sonnet-4-6` with `max_tokens: 16384`
- Workspace: `.` (relative, for portability)
- Bash denylist: `rm`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd`
- Cycle hooks: `syntax`, `static`, `import_smoke`
- Import smoke modules: `harness.core.config`, `harness.core.llm`, `harness.tools`
- Auto-commit: yes, auto-push: no
- 50 cycles max, 20 notes cycles
- Output: `harness_output` (relative)

### agent_example_self_improve_server.json (server deploy template)

- Model: `deepseek-chat` with `max_tokens: 8000` via `base_url: https://api.deepseek.com/anthropic`
- Workspace: `/home/ubuntu/harness-everything`
- **Tool restrictions**: Explicit `allowed_tools` list that DELIBERATELY excludes `batch_write` and `batch_edit` because DeepSeek-Chat cannot reliably emit their nested-array JSON schema. `batch_read` is kept (simpler schema). `write_file` / `edit_file` provide single-path fallback
- Extra tools: `write_file`, `edit_file`
- Bash denylist: `rm`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd`, `git` (git mutations handled by harness plumbing; read-only git via dedicated tools)
- Cycle hooks: `syntax`, `static`, `import_smoke`
- Import smoke modules: `harness.core.config`, `harness.core.llm`, `harness.core.signal_util`, `harness.core.hooks`, `harness.tools`, `harness.tools.path_utils`, `harness.agent`
- 20 cycles per chunk, then exit. Auto-tag at cycle 20 with prefix `harness-r`, push tag to trigger deploy workflow
- Auto-commit: yes, auto-push: yes (remote `origin`, branch `main`)
- Auto-tag: interval `20`, prefix `harness-r`, push `true`
- Output: `/home/ubuntu/harness-everything/harness_output`

### agent_self_improve.json (local continuous)

- Model: `bedrock/claude-sonnet-4-6` with `max_tokens: 16384` via `base_url: http://127.0.0.1:9099`
- Workspace: `/home/user/harness/Harness-Everything`
- Continuous mode: `true`, 999 max cycles, 30 notes cycles
- Bash denylist: `rm`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd`
- Cycle hooks: `syntax`, `static`, `import_smoke`
- Import smoke modules: `harness.core.config`, `harness.core.llm`, `harness.core.security`, `harness.core.signal_util`, `harness.core.hooks`, `harness.tools`, `harness.tools.path_utils`, `harness.agent`, `harness.agent.cycle_metrics`, `harness.evaluation.dual_evaluator`
- Auto-commit: yes, auto-push: no
- Output: `/home/user/harness/harness_output`

### agent_example_project.json (external project agent)

- Model: `vertex/claude-sonnet-4-6` with `max_tokens: 16384` via `base_url: http://127.0.0.1:9099`
- Workspace: `/home/user/harness/ExampleProject`
- Branch: `feat/harness` (switched by external `harness-gdc.sh` script before launch)
- Continuous mode: `true`, 999 max cycles, 30 notes cycles
- Bash denylist: `rm`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd`, `docker`, `docker-compose`
- Cycle hooks: `syntax`, `static` only (NO `import_smoke` -- `bridge.*` packages import `psycopg2` at module level and need a live DB)
- Syntax check patterns: `bridge/**/*.py` only
- Auto-evaluate: `true`, meta-review interval: `5` cycles
- Auto-commit: yes, auto-push: no (push is manual via `harness-gdc.sh`)
- Output: `/home/user/harness/harness_output`

---

## Bash Command Denylist Patterns

Common denylist across all configs: `rm`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd`

Additional per-config:
- Server config adds `git` (use dedicated git tools instead)
- ExampleProject adds `docker`, `docker-compose`
