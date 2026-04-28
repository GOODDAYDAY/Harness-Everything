# Technical Architecture

## Philosophy

Harness-Everything is an autonomous coding agent that improves codebases through iterative cycles. The design prioritizes:

- **Single-agent simplicity**: One LLM with full tool access, not a multi-agent pipeline
- **Crash safety**: Every cycle is independently resumable; no work is lost on interruption
- **Self-improvement safety**: The agent can modify its own source code, so verification hooks gate every commit
- **Provider agnosticism**: Any Anthropic-compatible API works (Claude, DeepSeek, etc.)

## Architecture Overview

The system runs in a loop: execute a cycle, verify the output, evaluate quality, commit if safe, then decide whether to continue. Each cycle gets a fresh system prompt with accumulated context (notes, scores, project state). The evaluation system uses two independent LLM evaluators that never see each other's output, producing a weighted composite score.

Tools provide the agent's interface to the filesystem, shell, and codebase analysis. All file operations pass through a security layer that confines access to the configured workspace. Deployment is handled by systemd + GitHub Actions, with automatic rollback on failed smoke tests.

## Key Decisions

| Date | Decision | Rationale | How to Extend |
|:---|:---|:---|:---|
| 2025 | Single agent loop over multi-agent pipeline | Simpler to debug, cheaper to run, easier to resume | Add phases within the cycle, not additional agents |
| 2025 | Dual isolated evaluation | Prevents groupthink; basic measures quality, diffusion measures safety | Add a third evaluator by extending the scoring combination |
| 2025 | Selective file staging over `git add -A` | Avoids committing unintended files (temp files, logs) | Changed paths are tracked per-cycle and staged explicitly |
| 2025 | Batch tools as primary, single-file as secondary | Reduces LLM round-trips; one call reads 50 files | New tools should prefer batch interfaces |
| 2025 | Security via path resolution + O_NOFOLLOW + post-open validation | Defense in depth against symlink/traversal attacks | Add checks to the validation pipeline, don't bypass it |
| 2025 | Tool output compaction with signal-aware tiers | Preserves diagnostic value while fitting context window | Assign new tools to the appropriate signal tier |

## Extension Guide

- **Adding a tool**: Subclass the tool base, implement the schema and execute method, register in the tool list. If it touches files, enable path checking.
- **Adding an evaluation dimension**: Update the evaluator prompt template; the scoring/parsing infrastructure handles arbitrary dimensions.
- **Adding a verification hook**: Subclass the hook base, implement the run method, add to the hook list. Set the gating flag if failure should block commits.
- **Changing LLM provider**: Set the base URL and API key in the config JSON. No code changes needed.

## Glossary

### Agent Runtime

| Term | Definition | Location |
|:---|:---|:---|
| **Agent** | 拥有完整工具访问权的自主 LLM 实例，按 cycle 迭代执行 mission | `agent/agent_loop.py` — `AgentLoop` |
| **Cycle** | Agent 执行循环的一次迭代。包含：prompt 构建 → 工具对话 → hook 验证 → 评估 → 提交 → 产出物持久化。对外编号从 1 开始 | `agent/agent_loop.py` — `run()` |
| **Mission** | 用户下达的任务描述，每个 cycle 注入 system prompt。Agent 通过 `MISSION COMPLETE` 或 `MISSION BLOCKED` 信号终止 | `AgentConfig.mission` |
| **Tool Turn** | Agent 发起的一次工具调用及其返回。每个 cycle 最多 `max_tool_turns` 次（默认 60） | `HarnessConfig.max_tool_turns` |
| **Execution Log** | 每个 cycle 的工具调用记录列表：工具名、参数、成功/失败、输出、耗时。序列化为 `tool_log.json` | `agent/agent_loop.py` |
| **Agent Notes** | `agent_notes.md`，跨 cycle 持久化的笔记文件。每个 cycle 末尾追加摘要，checkpoint 时压缩旧条目。对话裁剪后仍存活 | `AgentLoop._notes_path` |
| **Continuous Mode** | `continuous=True` 时 Agent 不因 `MISSION COMPLETE` 停止，持续 cycle 直到 `max_cycles` 或手动终止 | `AgentConfig.continuous` |

### Configuration

| Term | Definition | Location |
|:---|:---|:---|
| **HarnessConfig** | 中心配置 dataclass。涵盖 LLM 参数（model, max_tokens, base_url, api_key）、工作区（workspace, allowed_paths）、安全（homoglyph blocklist）、工具（allowed_tools, extra_tools）、执行（max_tool_turns, max_concurrent_llm_calls） | `core/config.py` |
| **AgentConfig** | Agent 模式配置，组合 HarnessConfig 并扩展：mission, max_cycles, cycle_hooks, auto_commit, auto_evaluate, meta_review_interval, auto_squash, auto_tag, extra | `agent/agent_loop.py` |
| **Workspace** | Agent 工作的根目录。所有文件操作和 git 操作相对于此路径。由 path security 层强制约束 | `HarnessConfig.workspace` |
| **Allowed Paths** | Agent 可访问的目录白名单。默认 `[workspace]`。所有文件工具通过 `_check_path()` 强制校验 | `HarnessConfig.allowed_paths` |
| **Extra Params** | `AgentConfig.extra` — 项目特定参数字典，框架不解释，原样注入 system prompt（如编码规范、领域术语、禁止模式） | `AgentConfig.extra` |

### Tool System

| Term | Definition | Location |
|:---|:---|:---|
| **Tool** | Agent 可调用的能力单元。每个 Tool 是 `Tool` 子类，实现 `name`, `description`, `input_schema()`, `async execute()` | `tools/base.py` |
| **ToolResult** | 工具统一返回结构。字段：`output`（成功文本）、`error`（错误文本）、`is_error`（布尔）、`elapsed_s`（耗时）、`metadata`（结构化数据）、`images`（base64 图像块） | `tools/base.py` |
| **Tool Registry** | 工具名 → Tool 实例的内存映射。启动时由 `build_registry()` 一次性构建。运行时做参数归一化、别名重写、schema 校验 | `tools/registry.py` |
| **Tool Tags** | 工具分类标签（如 `execution`, `file_read`, `git`, `game`）。用于按标签过滤工具集 | `Tool.tags` |
| **Tool Aliases** | 参数名归一化映射。LLM 常发 `file_path` 而 schema 期望 `path`，registry 在分发前自动重写 | `registry.py` — `_PARAM_ALIASES` |
| **Path Security** | 文件类工具的安全门。检查链：null 字节 → 控制字符 → Unicode 同形字 → 符号链接逃逸 → 路径边界 | `core/security.py` |
| **Bash Denylist** | bash 工具的命令黑名单。按 shell 链段（`&&`, `\|\|`, `;`, `\|`, `&`）拆分后逐段检查首个 token | `HarnessConfig.bash_command_denylist` |

### Hook System

| Term | Definition | Location |
|:---|:---|:---|
| **Hook** | 工具循环结束后的验证门控。在 commit 之前运行。分为 gating（阻断提交）和 advisory（仅报告） | `core/hooks.py` — `VerificationHook` |
| **Gating Hook** | `gates_commit=True` 的 hook。失败时阻止 git commit | `VerificationHook.gates_commit` |
| **HookResult** | Hook 统一返回结构。字段：`passed`（布尔）、`output`（成功文本）、`errors`（错误文本） | `core/hooks.py` |
| **SyntaxCheckHook** | 对匹配 glob 的文件执行 `py_compile`。Gating | `core/hooks.py` |
| **StaticCheckHook** | 对变更的 `.py` 文件执行 ruff 或 pyflakes（F821/F811/F401）。Gating | `core/hooks.py` |
| **ImportSmokeHook** | 子进程导入配置的模块 + 可选 smoke calls，检测 NameError/SyntaxError。Gating | `core/hooks.py` |
| **GodotSyntaxHook** | 调用 Godot headless 验证 GDScript 语法。Gating。可选 | `core/hooks.py` |
| **GameSmokeHook** | 启动游戏验证进程存活、状态合法、可截图。Gating。可选 | `core/hooks.py` |

### Evaluation

| Term | Definition | Location |
|:---|:---|:---|
| **DualEvaluator** | 双隔离评估器。两个独立 LLM 调用并行评分，互不可见输出。结果按 60% basic + 40% diffusion 加权合并 | `evaluation/dual_evaluator.py` |
| **Basic Evaluator** | 正确性评估。4 维度：Correctness (40%), Completeness (30%), Specificity (20%), Architecture Fit (10%)。产出 TOP DEFECT + ACTIONABLE FEEDBACK | evaluator prompt |
| **Diffusion Evaluator** | 系统影响评估。4 维度：Caller Impact, Maintenance Debt, Emergent Behaviour, Rollback Safety。关注二阶效应和生态影响 | evaluator prompt |
| **DualScore** | 双评估结果。字段：`basic`（ScoreItem）、`diffusion`（ScoreItem）。属性 `combined` 计算加权分 | `evaluation/dual_evaluator.py` |
| **ScoreItem** | 单评估器结果。字段：`score`（0-10 浮点）、`critique`（文本反馈） | `evaluation/dual_evaluator.py` |
| **Evaluator Mode** | 评估上下文模式。**implement**：评估已执行的代码变更；**reasoning**：评估探索/决策质量（无代码变更）；**debate**：评估文本提案 | `DualEvaluator.evaluate(mode=)` |
| **Score Range** | 0-10 浮点。锚点：0=损坏/危险，3-5=部分完成，6-7=可用/小问题，8=具体+可测试，9=已测试+正确，10=已测试+正确+可度量 | evaluator prompt |
| **Calibration Anchors** | 注入评估器 prompt 的校准短语，确保两个评估器使用一致的评分参考系 | `evaluation/dual_evaluator.py` |
| **Score History** | cycle 评分记录列表（cycle, basic, diffusion, combined）。上限 50 条。checkpoint 时格式化为 markdown 表格 | `agent/agent_eval.py` |

### Checkpoint

| Term | Definition | Location |
|:---|:---|:---|
| **Checkpoint** | 周期性战略审查点。启动时（cold）和每 `meta_review_interval` 个 cycle 执行。分析评分趋势 + git 历史 + 笔记，产出战略指导 | `agent/agent_eval.py` — `run_checkpoint()` |
| **Cold Checkpoint** | 启动时的首次 checkpoint（cycle=-1）。基于历史笔记和 git 状态为 Agent 设定初始方向。不执行 squash/tag | `agent/agent_eval.py` |
| **Meta-Review** | Checkpoint 的核心 LLM 调用。分析评分趋势 + git delta + 笔记，产出 `meta_context` 注入后续 cycle 的 system prompt | `agent/agent_eval.py` — `_meta_review_llm()` |
| **Notes Compression** | Checkpoint 时 LLM 压缩旧笔记条目，保留最近 10 条原文。控制 `agent_notes.md` 的上下文膨胀 | `agent/agent_eval.py` — `_compress_notes_llm()` |
| **CheckpointResult** | Checkpoint 返回结构。字段：`meta_context`（战略指导文本）、`head_hash`（当前 HEAD）、`squashed`（是否执行了 squash）、`tagged`（tag 名称） | `agent/agent_eval.py` |

### Git Operations

| Term | Definition | Location |
|:---|:---|:---|
| **Stage** | `git add -- <paths>` 标记文件为待提交。仅 stage `changed_paths` 中的文件，非 `git add -A` | `agent/agent_git.py` — `stage_changes()` |
| **Changed Paths** | 从工具调用日志提取的被修改文件路径列表。仅统计文件写入类工具（write, edit, patch 等） | `tools/path_utils.py` — `collect_changed_paths()` |
| **Staged Diff** | `git diff --cached`，暂存区中待提交的变更。用于评估器输入 | `agent/agent_git.py` — `get_staged_diff()` |
| **Committed Diff** | `git diff <hash>..HEAD`，两个 commit 之间的变更。当 Agent 在工具循环中自行 commit 导致暂存区为空时的回退方案 | `agent/agent_git.py` — `get_committed_diff()` |
| **Git Delta** | `git log --oneline` + `git diff --stat` 的组合摘要。注入 checkpoint 上下文，让 LLM 了解已完成的工作 | `agent/agent_git.py` — `get_review_git_delta()` |
| **Squash** | 将多个 commit 合并为一个。Checkpoint 时由 LLM 按逻辑任务分组，然后 programmatic interactive rebase 执行 | `agent/agent_git.py` — `squash_groups()` |
| **Auto Push** | 成功 commit 后自动 `git push`。`auto_push_remote`（默认 origin）+ `auto_push_branch`（默认 main） | `AgentConfig.auto_push` |

### Artifacts & Persistence

| Term | Definition | Location |
|:---|:---|:---|
| **ArtifactStore** | 分层运行产出物管理器。目录结构：`output_dir/run_id/cycle_N/`。方法：`path()`, `write()`, `read()`, `exists()`, `write_final_summary()` | `core/artifacts.py` |
| **Run ID** | 运行唯一标识。格式：`run_<YYYYMMDDTHHMMSS>`。用作 `run_dir` 目录名 | `ArtifactStore` |
| **Cycle Artifacts** | 每个 cycle 持久化到 `cycle_N/` 的产出：`output.txt`（Agent 文本输出）、`tool_log.json`（工具调用日志）、`eval_scores.json`（评分）、`hook_failures.txt`（hook 失败） | `AgentLoop._persist_cycle()` |
| **Final Summary** | `final_summary.md`，运行结束时写入。包含 mission_status, cycles_run, total_tool_calls。其存在标志运行已完结 | `ArtifactStore.write_final_summary()` |
| **Resumable Run** | `find_resumable()` 检测无 `final_summary.md` 的最新 run_dir，视为中断的运行可恢复 | `ArtifactStore.find_resumable()` |

### Control Flow

| Term | Definition | Location |
|:---|:---|:---|
| **MISSION COMPLETE** | Agent 在输出文本中发出的终止信号（大小写不敏感子串匹配）。触发正常退出。Continuous 模式下忽略 | `agent/agent_loop.py` |
| **MISSION BLOCKED** | Agent 发出的阻塞信号，表明遇到无法自主解决的障碍（缺凭据、需人类决策等）。立即终止 | `agent/agent_loop.py` |
| **Pause File** | 默认 `.harness.pause`。文件存在时 Agent 完成当前 cycle 后休眠（每 30s 轮询），删除后恢复。允许无杀进程的暂停/恢复 | `AgentConfig.pause_file` |
| **Graceful Shutdown** | SIGINT/SIGTERM 处理。设置 `_shutdown_requested` 标志，Agent 完成当前 cycle 后退出 | `core/signal_util.py` |
| **Mission Status** | 运行最终状态枚举：`complete`（任务完成）、`blocked`（遇阻）、`partial`（信号中断）、`exhausted`（达到 max_cycles） | `AgentResult.mission_status` |

### LLM Client

| Term | Definition | Location |
|:---|:---|:---|
| **LLM** | Anthropic Claude API 封装。提供重试、对话裁剪、并发控制 | `core/llm.py` |
| **Conversation Pruning** | 对话超过 300K 字符时，截断旧的 tool_result 文本（保留最近 3 轮完整）。保持 API 消息结构（每个 tool_use 配对 tool_result） | `core/llm.py` |
| **Retry Policy** | 可重试错误（rate limit, overload, timeout）的指数退避。最多 4 次重试（共 5 次尝试），含 jitter | `core/llm.py` |
| **Scratchpad** | LLM 工具循环中被拦截的特殊工具。Agent 写入笔记，循环将笔记注入每轮 system prompt 顶部。最多 30 条 | `core/llm.py` |
| **Context Budget** | 被拦截的特殊工具。返回当前 token 用量、轮次计数、scratchpad 大小，帮助 Agent 决定何时收尾 | `core/llm.py` |

### Metrics

| Term | Definition | Location |
|:---|:---|:---|
| **CycleMetrics** | 每 cycle 采集的 7 轴质量指标：工具效率、产出质量、执行健康、冗余度、行为信号、上下文质量、记忆与学习 | `agent/cycle_metrics.py` |
| **Tool Efficiency** | 工具调用次数、分布、成功率 | `cycle_metrics.py` |
| **Redundancy** | 同一文件的重复读取次数。高冗余暗示 Agent 在循环而非推进 | `cycle_metrics.py` |
| **Behaviour Signals** | scratchpad 使用、测试运行、lint 检查的频率。反映 Agent 的工作习惯质量 | `cycle_metrics.py` |
