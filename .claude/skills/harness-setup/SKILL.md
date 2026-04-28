---
name: harness-setup
description: Set up Harness-Everything to autonomously improve a new target project — config, script, monitoring, cleanup
argument-hint: "[project-name]"
---

# harness-setup
> Version: v1 | Date: 2026-04-28

## 1. Overview

为一个新的目标项目配置 Harness-Everything 自动化迭代。完成后你将获得：

1. **Agent 配置** — `config/agent_<project>.json`（mission、工具、hooks、评估策略）
2. **控制脚本** — `harness-<project>.sh`（start/stop/pause/resume/status/logs 六件套）
3. **监控与清理知识** — 如何判断产出质量、如何 squash 推送

```
Harness 工作区布局:

harness/                          ← 工作区根目录
├── Harness-Everything/           ← 框架本体
│   ├── harness/cli.py            ← 入口: python -m harness.cli <config.json>
│   ├── config/
│   │   ├── agent_example.json    ← 模板（复制这个开始）
│   │   └── agent_<project>.json  ← 你的项目配置
│   └── .venv/                    ← Python 虚拟环境
├── <Project>/                    ← 目标项目（git 子目录）
├── harness-<project>.sh          ← 控制脚本（放工作区根目录）
├── harness_output/               ← 运行产出（自动生成）
└── docs/
    ├── harness-monitoring.md     ← 监控指南
    └── harness-output-cleanup.md ← 产出整理指南
```

## 2. 第一步：写配置 JSON

从 `config/agent_example.json` 复制一份，改名为 `agent_<project>.json`。

### 2.1 harness 节（运行时配置）

```jsonc
{
  "harness": {
    // ── 模型 ──
    "model": "claude-sonnet-4-6",              // 模型 ID（见下方模型选择表）
    "max_tokens": 16384,                        // 单次 LLM 输出上限
    "base_url": "http://<PROXY_HOST>:<PROXY_PORT>",       // 代理地址（空串=直连 Anthropic）
    "api_key": "",                              // 留空，通过 HARNESS_API_KEY 环境变量注入

    // ── 工作空间 ──
    "workspace": "/absolute/path/to/<Project>",
    "allowed_paths": ["/absolute/path/to/<Project>"],

    // ── 工具 ──
    "allowed_tools": [],       // 空=使用全部默认工具
    "extra_tools": [],         // 可选: ["db_query", "web_search", "http_request"]
    "tool_config": {           // extra_tools 的配置
      // "db_query": { "dsn": "postgresql://...", "timeout": 30, "max_rows": 500 }
    },
    "bash_command_denylist": [
      "rm", "shutdown", "reboot", "poweroff", "mkfs", "dd",
      "docker", "docker-compose"
    ],

    // ── 并发与深度 ──
    "max_tool_turns": 200,             // 单 cycle 最大工具调用轮数
    "max_concurrent_llm_calls": 4,     // 并行 LLM 调用数（评估时用）
    "log_level": "INFO"
  }
}
```

### 2.1b 模型选择与 Provider 配置

Harness 底层使用 `anthropic.AsyncAnthropic` SDK。任何兼容 Anthropic Messages API 的 provider 都可以通过 `base_url` 接入。

#### 可用模型一览

| Provider | model 值 | base_url | 上下文 | max_tokens 建议 | 适用场景 |
|----------|---------|----------|--------|----------------|---------|
| **Anthropic 直连** | `claude-sonnet-4-6` | `""` (留空) | 200K | 16384 | 默认选择，性价比最高 |
| **Anthropic 直连** | `claude-opus-4-6` | `""` (留空) | 200K | 16384 | 需要深度推理的复杂 mission |
| **Anthropic 直连** | `claude-haiku-4-5-20251001` | `""` (留空) | 200K | 8192 | 轻量任务、高频评估 |
| **Anthropic via 代理** | `vertex/claude-sonnet-4-6` | `http://<PROXY_HOST>:<PROXY_PORT>` | 200K | 16384 | 通过本地代理路由 |
| **DeepSeek** | `deepseek-v4-flash` | `https://api.deepseek.com/anthropic` | 1M | 16384 | 超长上下文 + 极致性价比 |
| **DeepSeek** | `deepseek-v4-pro` | `https://api.deepseek.com/anthropic` | 1M | 16384 | 超长上下文 + 高质量推理 |

#### Provider 配置示例

**Anthropic 直连（最简单）：**
```jsonc
{
  "model": "claude-sonnet-4-6",
  "base_url": "",                    // 留空 = 直连 api.anthropic.com
  "api_key": ""                      // 通过 HARNESS_API_KEY 注入
}
// HARNESS_API_KEY=sk-ant-xxx
```

**Anthropic via 代理（内部路由）：**
```jsonc
{
  "model": "vertex/claude-sonnet-4-6",
  "base_url": "http://<PROXY_HOST>:<PROXY_PORT>",
  "api_key": ""                      // 从环境变量注入
}
```

**DeepSeek（Anthropic 兼容端点）：**
```jsonc
{
  "model": "deepseek-v4-flash",      // 或 "deepseek-v4-pro"
  "base_url": "https://api.deepseek.com/anthropic",
  "api_key": ""                      // HARNESS_API_KEY=sk-deepseek-xxx
}
// 注意：必须用 /anthropic 端点，不是 OpenAI 格式的根路径
```

#### DeepSeek 模型详解

| | DeepSeek-V4-Flash | DeepSeek-V4-Pro |
|---|---|---|
| **上下文** | 1M tokens | 1M tokens |
| **最大输出** | 384K tokens | 384K tokens |
| **思考模式** | 支持（默认开启） | 支持（默认开启） |
| **Tool Calls** | 支持 | 支持 |
| **输入价格** (缓存命中) | 0.02 元/M tokens | 0.1 元/M tokens |
| **输入价格** (缓存未命中) | 1 元/M tokens | 12 元/M tokens |
| **输出价格** | 2 元/M tokens | 24 元/M tokens |
| **适合** | 日常迭代、大量 cycle 跑批 | 复杂推理、高质量产出 |

**DeepSeek 的优势场景：**
- 目标项目代码量大（>100K 行），需要 1M 上下文窗口
- 预算敏感，Flash 的成本约为 Claude Sonnet 的 1/10
- 长时间连续运行（continuous=true），累计 token 消耗大

**DeepSeek 的注意事项：**
- Tool calls 质量可能不如 Claude，复杂工具编排场景建议先小规模测试
- `max_tokens` 可以设更大（如 32768），因为输出上限是 384K
- 思考模式默认开启，会消耗额外 token 但提升推理质量

#### 混合使用策略

可以为不同用途选不同模型——主 agent 用一个，评估/meta-review 用另一个（需要在代码层面支持，当前配置是全局统一模型）。

**推荐组合：**

| 场景 | 推荐模型 | 理由 |
|------|---------|------|
| 初次接入新项目（探索期） | `claude-sonnet-4-6` | 工具调用准确、产出稳定 |
| 日常维护（长跑） | `deepseek-v4-flash` | 成本极低、上下文够大 |
| 关键重构/复杂 bug | `claude-opus-4-6` | 深度推理能力强 |
| 大型项目 + 预算充裕 | `deepseek-v4-pro` | 1M 上下文 + 高质量 |
| 轻量验证/评估 | `claude-haiku-4-5-20251001` | 快速便宜 |

**关键决策点：**

| 字段 | 怎么选 |
|------|--------|
| `model` | 见上方模型选择表，按场景选 |
| `base_url` | Anthropic 直连留空；用代理填代理地址；DeepSeek 填 `https://api.deepseek.com/anthropic` |
| `api_key` | 留空通过 `HARNESS_API_KEY` 环境变量注入，或在脚本 `ensure_api_key` 中配置 |
| `extra_tools` | 有生产数据库 → `["db_query"]`；需要联网 → `["web_search"]` |
| `max_tool_turns` | 200 够用，600 浪费 token，50 太短复杂任务跑不完 |
| `max_concurrent_llm_calls` | 本地开发 4 就够，有配额可以拉到 20 |

### 2.2 mission 字段（最重要）

Mission 是 agent 每个 cycle 的系统提示词。写得好不好直接决定产出质量。

**结构模板：**

```text
You are the autonomous maintainer of <Project> — <一句话描述项目>.

Your standing mission: <核心目标>.

═══════════════════════════════════════════════════════
ENVIRONMENT
═══════════════════════════════════════════════════════

  * Workspace: <路径> (branch: <分支>)
  * 运行环境约束（DB 是否可用、哪些依赖缺失等）

═══════════════════════════════════════════════════════
Architecture overview
═══════════════════════════════════════════════════════

<项目架构概述：入口 → 核心模块 → 数据层>

Key modules:
  <path>  — <职责>
  <path>  — <职责>
  ...

═══════════════════════════════════════════════════════
Improvement axes — rotate between them
═══════════════════════════════════════════════════════

1. <轴1> — <描述 + 具体关注点>
2. <轴2> — <描述 + 具体关注点>
3. <轴3> — <描述 + 具体关注点>

═══════════════════════════════════════════════════════
Hard invariants — never violate
═══════════════════════════════════════════════════════

  <约束1>
  <约束2>
  ...

═══════════════════════════════════════════════════════
How to run one good cycle
═══════════════════════════════════════════════════════

  1. Read git log --oneline -10 to see what was recently changed
  2. Read your previous cycle notes for the 'Next high-value target'
  3. Pick ONE concrete target from the axes above
  4. Read the relevant files with batch_read BEFORE editing
  5. Make the change — keep it small and coherent
  6. Verify: <项目特定的验证方式>
  7. End with a clear 'Next high-value target' in your summary
```

**Mission 写作要点：**
- **具体到文件路径** — 不要写"改进代码质量"，写"bridge/context/assemblers/ 下的 prompt 常量"
- **给出判断标准** — 不要写"优化性能"，写"减少不必要的 LLM 调用（prompt_len > 8000 的 caller）"
- **标明禁区** — 明确告诉 agent 哪些事不该做（改业务逻辑、改配置阈值等）
- **鼓励数据驱动** — 如果有 `db_query` 工具，写出具体的诊断 SQL

### 2.3 Cycle 控制字段

```jsonc
{
  // ── 运行模式 ──
  "continuous": false,        // true=无限循环（维护模式），false=完成即停
  "max_cycles": 50,           // 最大 cycle 数（continuous=true 时设大值如 999）

  // ── 验证 hooks ──
  "cycle_hooks": ["syntax", "static", "import_smoke"],
  // syntax    — py_compile 检查语法
  // static    — ruff 静态检查
  // import_smoke — 尝试 import 指定模块（需要依赖能装上）
  // 任何一个 hook 失败 → cycle 不提交

  "syntax_check_patterns": ["src/**/*.py"],       // syntax hook 扫描范围
  "import_smoke_modules": ["mypackage.core"],     // import_smoke 检查的模块
  "import_smoke_calls": [],                        // 可选：调用特定函数验证

  // ── 自动评估 ──
  "auto_evaluate": true,          // 每个 cycle 结束后自动打分
  "meta_review_interval": 5,      // 每 5 个 cycle 做一次战略回顾
  "auto_squash": true,            // 回顾时自动 squash commits
  "auto_tag": false,              // 是否自动打 git tag

  // ── 提交 ──
  "auto_commit": true,            // hooks 通过后自动 git commit
  "commit_repos": ["."],          // 提交哪些仓库（相对于 workspace）
  "auto_push": false,             // 不自动 push（推荐手动控制）

  // ── 产出 ──
  "output_dir": "/path/to/harness_output"
}
```

**hooks 选择指南：**

| 项目特征 | 推荐 hooks |
|----------|-----------|
| 纯 Python，依赖能本地装 | `["syntax", "static", "import_smoke"]` |
| Python，但依赖需要远程服务（DB、MCP 等） | `["syntax", "static"]` |
| 非 Python 项目 | 自定义 hook 或只用 `["syntax"]` |

### 2.4 完整配置示例

```jsonc
{
  "//": "=== MyProject Agent ===",
  "harness": {
    "model": "vertex/claude-sonnet-4-6",
    "max_tokens": 16384,
    "base_url": "http://<PROXY_HOST>:<PROXY_PORT>",
    "api_key": "",
    "workspace": "/Users/me/harness/MyProject",
    "allowed_paths": ["/Users/me/harness/MyProject"],
    "allowed_tools": [],
    "extra_tools": [],
    "tool_config": {},
    "bash_command_denylist": ["rm", "shutdown", "reboot", "poweroff", "mkfs", "dd"],
    "max_tool_turns": 200,
    "max_concurrent_llm_calls": 4,
    "log_level": "INFO"
  },

  "mission": "You are the autonomous maintainer of MyProject...",

  "continuous": false,
  "max_cycles": 50,
  "cycle_hooks": ["syntax", "static"],
  "syntax_check_patterns": ["src/**/*.py"],

  "auto_evaluate": true,
  "meta_review_interval": 5,
  "auto_squash": true,
  "auto_tag": false,

  "auto_commit": true,
  "commit_repos": ["."],
  "auto_push": false,

  "output_dir": "/Users/me/harness/harness_output"
}
```

**DeepSeek 版配置示例（低成本长跑）：**

```jsonc
{
  "harness": {
    "model": "deepseek-v4-flash",
    "max_tokens": 32768,                                   // DeepSeek 输出上限 384K，可以设大
    "base_url": "https://api.deepseek.com/anthropic",      // Anthropic 兼容端点
    "api_key": "",
    "workspace": "/Users/me/harness/MyProject",
    "allowed_paths": ["/Users/me/harness/MyProject"],
    "allowed_tools": [],
    "extra_tools": [],
    "tool_config": {},
    "bash_command_denylist": ["rm", "shutdown", "reboot", "poweroff", "mkfs", "dd"],
    "max_tool_turns": 200,
    "max_concurrent_llm_calls": 4,
    "log_level": "INFO"
  },

  "mission": "You are the autonomous maintainer of MyProject...",

  "continuous": true,            // 长跑模式，配合 Flash 低成本
  "max_cycles": 999,
  "cycle_hooks": ["syntax", "static"],
  "syntax_check_patterns": ["src/**/*.py"],

  "auto_evaluate": true,
  "meta_review_interval": 5,
  "auto_squash": true,
  "auto_tag": false,

  "auto_commit": true,
  "commit_repos": ["."],
  "auto_push": false,

  "output_dir": "/Users/me/harness/harness_output"
}
// 启动: HARNESS_API_KEY=sk-deepseek-xxx ./harness-myproject.sh start
// 或在脚本 ensure_api_key 中配置
```

## 3. 第二步：写控制脚本

控制脚本是 harness 的操作界面。放在工作区根目录，命名为 `harness-<project>.sh`。

### 3.1 脚本骨架

以下是完整的控制脚本模板。复制后只需要改标注了 `# ← EDIT` 的行：

```bash
#!/usr/bin/env bash
# harness-<project>.sh — start / stop the Harness agent for <Project>
#
# Usage:
#   ./harness-<project>.sh start   — launch agent in background, tail logs
#   ./harness-<project>.sh stop    — gracefully stop the running process
#   ./harness-<project>.sh status  — show whether it is running
#   ./harness-<project>.sh logs    — tail the log file (Ctrl-C to detach)
#   ./harness-<project>.sh pause   — pause after current cycle (safe suspend)
#   ./harness-<project>.sh resume  — remove pause, let harness continue

set -euo pipefail

# ── Paths（只改这里） ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$SCRIPT_DIR/Harness-Everything"
PROJECT_DIR="$SCRIPT_DIR/<Project>"                # ← EDIT: 目标项目目录名
VENV="$HARNESS_DIR/.venv"
PYTHON="$VENV/bin/python"

AGENT_CONFIG="$HARNESS_DIR/config/agent_<project>.json"  # ← EDIT: 配置文件名

PID_FILE="$SCRIPT_DIR/.harness-<project>.pid"      # ← EDIT: 进程文件名
LOG_FILE="$SCRIPT_DIR/.harness-<project>.log"      # ← EDIT: 日志文件名
PAUSE_FILE="$PROJECT_DIR/.harness.pause"

# ── Helpers ───────────────────────────────────────────────────────────

die() { echo "❌  $*" >&2; exit 1; }

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

ensure_proxy() {
    # Anthropic 直连或 DeepSeek 不需要代理 → 注释掉整个函数体，只留 return 0
    local proxy_pid
    proxy_pid=$(lsof -i :<PROXY_PORT> -sTCP:LISTEN -t 2>/dev/null || true)

    if [[ -n "$proxy_pid" ]]; then
        echo "🔄  Restarting local-proxy..."
        kill "$proxy_pid" 2>/dev/null || true
        sleep 0.5
    else
        echo "🔄  local-proxy not detected on :<PROXY_PORT> — starting..."
    fi

    python3 ~/local-proxy \
        --upstream <UPSTREAM_API_URL> \
        --port <PROXY_PORT> &>/tmp/local-proxy.log &
    disown
    sleep 0.8
    lsof -i :<PROXY_PORT> -sTCP:LISTEN -t &>/dev/null \
        || die "local-proxy failed to start. Check /tmp/local-proxy.log"
    echo "✅  local-proxy started"
}

ensure_api_key() {
    # ── 根据你的 provider 选择一种方式 ──

    # 方式 A：从 Apps Studio 读取（local-proxy / Vertex 路由）
    local db_path="$HOME/.apps-studio/apps-studio.db"
    [[ -f "$db_path" ]] || die "Apps Studio DB not found: $db_path"
    local key
    key=$(sqlite3 "$db_path" \
        "SELECT value FROM __kv__ WHERE key='llm.apiKey'" 2>/dev/null) \
        || die "Failed to read API key from Apps Studio DB"
    [[ -n "$key" ]] || die "llm.apiKey is empty"
    export HARNESS_API_KEY="$key"
    echo "✅  API key loaded (${key:0:8}...${key: -4})"

    # 方式 B：Anthropic 直连
    # export HARNESS_API_KEY="$ANTHROPIC_API_KEY"            # 从环境继承
    # export HARNESS_API_KEY="$(cat ~/.secrets/anthropic-key)"  # 从文件读取

    # 方式 C：DeepSeek
    # export HARNESS_API_KEY="$DEEPSEEK_API_KEY"             # 从环境继承
    # export HARNESS_API_KEY="$(cat ~/.secrets/deepseek-key)"   # 从文件读取

    # 方式 D：任意 provider（通用）
    # [[ -n "${HARNESS_API_KEY:-}" ]] || die "HARNESS_API_KEY not set"
    # echo "✅  API key from environment"
}

ensure_branch() {
    # 可选：确保目标项目在指定分支
    local target_branch="feat/harness"               # ← EDIT: 目标分支
    local branch
    branch=$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null) \
        || die "Not a git repo: $PROJECT_DIR"
    if [[ "$branch" != "$target_branch" ]]; then
        echo "🔄  Switching to $target_branch..."
        git -C "$PROJECT_DIR" checkout "$target_branch" \
            || die "Failed to checkout $target_branch"
    fi
    echo "✅  On branch: $target_branch"
}

# ── Commands ──────────────────────────────────────────────────────────

cmd_start() {
    is_running && die "Already running (PID $(cat "$PID_FILE")). Run '$0 stop' first."
    [[ -f "$PAUSE_FILE" ]] && die "Paused. Run '$0 resume' first."
    [[ -x "$PYTHON" ]] || die "Python venv not found: $PYTHON"
    [[ -f "$AGENT_CONFIG" ]] || die "Config not found: $AGENT_CONFIG"

    ensure_api_key
    ensure_proxy       # Anthropic 直连或 DeepSeek 不需要代理，注释掉
    ensure_branch      # 不需要切分支则注释掉

    echo ""
    echo "🚀  Starting Harness → <Project>"                # ← EDIT
    echo "    config : $AGENT_CONFIG"
    echo "    log    : $LOG_FILE"
    echo ""

    # caffeinate -i 防止 macOS 休眠时节流 asyncio
    nohup caffeinate -i "$PYTHON" -m harness.cli "$AGENT_CONFIG" \
        >"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    sleep 1
    if ! is_running; then
        rm -f "$PID_FILE"
        echo "💥  Crashed immediately. Last lines:"
        tail -20 "$LOG_FILE"
        exit 1
    fi

    echo "✅  Started (PID $(cat "$PID_FILE"))"
    echo ""
    echo "📋  Tailing logs (Ctrl-C to detach — harness keeps running):"
    echo "────────────────────────────────────────────────────────────"
    tail -f "$LOG_FILE"
}

cmd_stop() {
    if ! is_running; then
        echo "ℹ️   Not running."
        rm -f "$PID_FILE"
        return
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo "🛑  Stopping (PID $pid)..."

    # 三级停机：SIGINT(120s) → SIGTERM(30s) → SIGKILL
    kill -INT "$pid" 2>/dev/null || true
    echo "  SIGINT sent — waiting for current cycle to finish..."
    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        (( waited++ ))
        (( waited % 5 == 0 )) && printf "  ... %ds\n" "$waited"
        if (( waited == 120 )); then
            echo "⚠️   120s — escalating to SIGTERM..."
            kill -TERM "$pid" 2>/dev/null || true
        fi
        if (( waited == 150 )); then
            echo "⚠️   150s — SIGKILL..."
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
            break
        fi
    done
    rm -f "$PID_FILE"
    echo "✅  Stopped. (${waited}s)"
}

cmd_status() {
    if is_running; then
        echo "✅  Running (PID $(cat "$PID_FILE"))"
        echo "   log: $LOG_FILE"
        echo ""
        echo "📋  Last 10 lines:"
        echo "────────────────────────────────────────────────────────────"
        tail -10 "$LOG_FILE" 2>/dev/null || echo "  (no log yet)"
    else
        echo "⛔  Not running"
        [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    fi
}

cmd_logs() {
    [[ -f "$LOG_FILE" ]] || die "No log file: $LOG_FILE"
    echo "📋  Tailing $LOG_FILE (Ctrl-C to detach):"
    echo "────────────────────────────────────────────────────────────"
    tail -f "$LOG_FILE"
}

cmd_pause() {
    is_running || die "Not running."
    if [[ -f "$PAUSE_FILE" ]]; then
        echo "⏸️   Already paused."
        return
    fi
    touch "$PAUSE_FILE"
    echo "⏸️   Pause requested — will pause after current cycle."
}

cmd_resume() {
    if [[ ! -f "$PAUSE_FILE" ]]; then
        echo "ℹ️   Not paused."
        return
    fi
    rm -f "$PAUSE_FILE"
    echo "▶️   Resumed."
}

# ── Dispatch ──────────────────────────────────────────────────────────

case "${1:-}" in
    start)  cmd_start   ;;
    stop)   cmd_stop    ;;
    pause)  cmd_pause   ;;
    resume) cmd_resume  ;;
    status) cmd_status  ;;
    logs)   cmd_logs    ;;
    *)
        echo "Usage: $(basename "$0") {start|stop|pause|resume|status|logs}"
        echo ""
        echo "  start   — launch harness agent, then tail logs"
        echo "  stop    — gracefully stop (SIGINT → SIGTERM → SIGKILL)"
        echo "  pause   — finish current cycle then wait"
        echo "  resume  — remove pause, continue next cycle"
        echo "  status  — show PID + last 10 log lines"
        echo "  logs    — tail the log file (Ctrl-C to detach)"
        exit 1
        ;;
esac
```

### 3.2 脚本要改的地方清单

| 位置 | 改什么 | 示例 |
|------|--------|------|
| `PROJECT_DIR=` | 目标项目目录 | `$SCRIPT_DIR/MyProject` |
| `AGENT_CONFIG=` | 配置文件路径 | `agent_myproject.json` |
| `PID_FILE=` / `LOG_FILE=` | 进程/日志文件名 | `.harness-myproject.pid` |
| `ensure_branch` 里的 `target_branch` | 目标分支 | `feat/harness` |
| `ensure_proxy` | 不需要代理就注释掉 | — |
| `cmd_start` 的 echo | 启动提示 | `Starting Harness → MyProject` |

### 3.3 脚本核心设计

```
启动流程:
  check not running → check venv → load API key → start proxy → switch branch
  → nohup caffeinate -i python -m harness.cli <config> → save PID → tail logs

停机流程 (三级优雅停机):
  SIGINT  → agent 完成当前 cycle 后退出（等 120s）
  SIGTERM → 立即中断工具调用，保存状态（再等 30s）
  SIGKILL → 强杀（最后手段）

暂停机制:
  touch .harness.pause → agent 每个 cycle 结束时检查 → 存在就 sleep
  rm .harness.pause    → agent 下次检查时恢复
```

## 4. 第三步：初始化运行环境

```bash
# 1. 确保 Harness-Everything 的 venv 存在
cd Harness-Everything
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 确保目标项目在 harness 分支
cd ../<Project>
git checkout -b feat/harness   # 或切到已有分支

# 3. 给脚本加执行权限
chmod +x harness-<project>.sh

# 4. 启动
./harness-<project>.sh start
```

## 5. 运行时：监控

### 5.1 日常检查

```bash
./harness-<project>.sh status     # 快速看活没活
./harness-<project>.sh logs       # 持续跟踪
tail -80 .harness-<project>.log   # 看最近活动
```

### 5.2 日志信号解读

**正常信号：**
- `tool_loop turn=N` — cycle 进度
- `in_tok=N` — 上下文占用（接近 200K = 快到窗口上限）
- `compacted N old tool-result block(s)` — 上下文压缩，正常
- `batch_edit` → `lint_check` → `git add/commit` — 正常的编辑→验证→提交

**异常信号：**
- 长时间无新日志 — 进程可能卡住
- 反复 `batch_edit` 同一个文件 — 死循环
- `success: false` 持续出现 — 工具执行失败

### 5.3 产出评估三维度

对每个 harness 产出的提交，问三个问题：

| 维度 | 问什么 | 红旗 |
|------|--------|------|
| **有效性** | 解决了真实问题？ | "defense-in-depth"、"cosmetic"、含糊的 commit message |
| **合理性** | 修法正确？和现有模式一致？ | 一个小 bug 改了 5 个文件；引入不必要的复杂度 |
| **合适性** | 该 harness 来做？ | 改业务逻辑、改架构、改配置阈值 = 需要人确认 |

详见 `docs/harness-monitoring.md`。

## 6. 运行后：产出整理（Squash & Push）

Harness 产出大量 cycle 提交，需要整理后再推到目标分支。

### 6.1 分析

```bash
cd <Project>
git fetch origin

# 找到分叉基点
base=$(git merge-base feat/harness master)

# 看全部提交
git log --oneline $base..HEAD

# 分离实质提交 vs 空转 cycle
git log --format="%h %s" $base..HEAD | grep -v "agent: cycle"

# 统计
real=$(git log --oneline $base..HEAD | grep -cv "agent: cycle")
cycle=$(git log --oneline $base..HEAD | grep -c "agent: cycle")
echo "real: $real, cycle: $cycle"
```

### 6.2 Squash

```bash
# 建备份
git branch backup-before-squash HEAD

# 两步 reset（比 rebase -i 快且安全）
git reset --soft $base      # 所有变更 → 暂存区
git reset HEAD              # 暂存区 → 工作区

# 按功能分组提交（路径按项目调整）
git add src/core/ && git commit -m "refactor: ..."
git add src/api/  && git commit -m "feat: ..."
git add tests/    && git commit -m "test: ..."

# 验证（必须零输出）
git diff backup-before-squash
git status --short
```

### 6.3 推送

```bash
git push --force origin feat/harness
# 如果有目标分支:
# git push --force origin feat/harness:<target-branch>

# 确认后清理备份
git branch -D backup-before-squash
```

详见 `docs/harness-output-cleanup.md`。

## 7. 常见问题与调优

### 7.1 Agent 空转率高

- **表现**：cycle commit 无代码变更 > 50%
- **调整**：mission 太宽泛，缩小到具体文件/模块；或者项目确实没什么可改了

### 7.2 Agent 反复改同一个文件

- **表现**：日志中连续出现同一路径的 `batch_edit`
- **调整**：mission 中加 "Rotate axes. Don't spend 5+ consecutive cycles on the same file."

### 7.3 Hook 总是失败

- **syntax hook 失败**：检查 `syntax_check_patterns` 是否匹配了不该检查的文件
- **import_smoke 失败**：可能是依赖问题（DB、第三方服务），考虑关掉只保留 `["syntax", "static"]`
- **static hook 失败**：agent 不遵循 ruff 规则，mission 中加具体的 linting 约束

### 7.4 Token 消耗过快

- 减小 `max_tool_turns`（200 → 100）
- 减小 `max_concurrent_llm_calls`
- Mission 中强调 "keep changes small and coherent, one file at a time"

### 7.5 想给 agent 加新工具

内置工具（默认全开）：batch_read, batch_edit, batch_write, grep_search, glob_search, bash, git_status, git_diff, git_log, test_runner, lint_check, symbol_extractor, code_analysis, cross_reference, project_map, context_budget, tree, find_replace, diff_files, python_eval, json_transform, ast_rename, todo_scan, scratchpad, data_flow, call_graph, dependency_analyzer ...

可选工具（需在 `extra_tools` 中声明）：
- `db_query` — 查询数据库（需配置 DSN）
- `web_search` — DuckDuckGo 搜索
- `http_request` — HTTP 请求
- `git_search` — 深度 git 历史搜索

## 8. 执行步骤

当用户调用 `/harness-setup [project-name]` 时：

1. 从 `$ARGUMENTS` 获取项目名，没有则询问
2. 确认目标项目路径、分支、环境约束（DB？依赖？）
3. 询问 mission 的核心目标和改进轴
4. 复制 `config/agent_example.json` → `config/agent_<project>.json`，填入配置
5. 生成 `harness-<project>.sh` 控制脚本到工作区根目录
6. 引导用户完成初始化（venv、分支、chmod）
7. 提示用户 `./harness-<project>.sh start` 启动
