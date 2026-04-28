---
name: writing-skills
description: 如何为 Harness agent 编写 SKILL.md — 格式、字段、设计原则、示例
auto_load: false
---

# 编写 Harness Skills 指南

Skills 是 Harness agent 的多级上下文加载机制。每个 skill 是一个独立的知识文档，agent 可以自动加载或按需查询。

## 1. 文件位置与结构

```
<workspace>/.harness/skills/
  <skill-name>/
    SKILL.md          ← 唯一必需文件
```

- 每个 skill 是一个目录，目录名即 skill name
- 目录下只需要一个 `SKILL.md` 文件
- 命名规则：小写字母、数字、连字符（`a-z0-9-`）

## 2. SKILL.md 格式

```yaml
---
name: my-skill-name
description: "一行描述——出现在索引中，帮助 agent 判断是否需要加载"
auto_load: false
---

# Skill 正文（Markdown）

这里写具体的知识内容。
```

### Frontmatter 字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `name` | string | **是** | — | 与目录名一致，唯一标识 |
| `description` | string | **是** | — | 一行摘要。agent 根据这行决定是否加载，写得好不好直接影响命中率 |
| `auto_load` | bool | 否 | `false` | `true` = 每个 cycle 自动注入 system prompt |

### 关于 description

这是 agent 在索引中看到的**唯一信息**（除了 name）。写法决定了 agent 能否在正确的时机加载这个 skill：

- **好**：`GDC_Bots 数据库表结构、状态机转换、关键索引说明`
- **差**：`数据库相关内容`
- **好**：`step 执行失败时的诊断 SQL 和常见错误模式`
- **差**：`SQL 查询`

## 3. auto_load 的取舍

### auto_load: true — 每 cycle 自动注入

适合：
- 硬约束（"永远不要在 async def 里调用阻塞函数"）
- 架构概览（pipeline 流程图）
- 必须始终遵守的编码规范

**注意**：所有 auto_load skills 共享 **12,000 字符** 的预算。超预算的 skill 会被自动降级为 on-demand。

### auto_load: false — 按需加载（默认）

适合：
- 特定子系统的深度文档（DB schema、某个模块的内部实现）
- 诊断指南（特定错误的排查步骤）
- 参考资料（API 表格、配置说明）

Agent 通过 `skill_lookup(name="xxx")` 加载。

### 经验法则

> 如果 agent 每个 cycle 都需要看这个内容 → auto_load: true
> 如果只在处理特定问题时需要 → auto_load: false

## 4. 正文写作原则

### 4.1 写给 LLM 看，不是写给人看

- 用列表和表格，不要用大段散文
- 关键信息放在前面（LLM 对开头的注意力最高）
- 用具体的文件路径、函数名、命令，不要用模糊的描述

### 4.2 控制篇幅

| auto_load | 建议字数 | 原因 |
|-----------|---------|------|
| true | < 3,000 chars | 共享 12K 预算，留空间给其他 skill |
| false | < 8,000 chars | 太长的 tool_result 会浪费 context window |

超长内容拆成多个 skill，让 agent 按需逐个加载。

### 4.3 避免重复

不要把 CLAUDE.md、docs/、requirements/ 里已有的内容复制进 skill。Skill 应该是**补充性知识**——那些不在代码和文档里但 agent 需要知道的信息：

- 隐式约束（"别碰这个文件，它是手动管理的"）
- 诊断经验（"看到 X 错误通常意味着 Y"）
- 工作流指南（"改 prompt 前先查这几个表"）

## 5. 示例

### 示例 1：硬约束（auto_load: true）

```yaml
---
name: hard-invariants
description: "GDC_Bots 架构硬约束——DB 隔离、日志、异步安全、状态机"
auto_load: true
---

## Hard Invariants — 永不违反

- **DB ISOLATION** — SQL / psycopg2 只在 bridge/db/ 中出现，其他地方不允许
- **LOGGING** — 只用 `from loguru import logger`，禁止 print() 和 import logging
- **ASYNC SAFETY** — async def 中禁止阻塞调用（open()、time.sleep()、sync psycopg2）
- **STATE MACHINE** — Step 状态：pending→ready→running→done/failed/skipped
- **STATE PERSIST** — 每次状态变更必须立即调用 bridge/db/ 持久化
```

### 示例 2：诊断指南（auto_load: false）

```yaml
---
name: sql-diagnostics
description: "db_query 诊断 SQL——反馈分析、执行指标、prompt 膨胀、工具失败率"
auto_load: false
---

## 诊断 SQL 速查

### 低评分反馈
SELECT task_id, rating, comment FROM feedback
WHERE rating <= 2 ORDER BY created_at DESC LIMIT 10;

### 执行指标趋势
SELECT avg(reflection_rounds), avg(replanning_count),
  avg(CASE WHEN first_round_success THEN 1 ELSE 0 END) as first_round_rate
FROM task_metrics
WHERE task_id > (SELECT max(id)-50 FROM tasks);

### Prompt 膨胀检测
SELECT caller, count(*), avg(prompt_len)::int, avg(response_len)::int
FROM llm_call_logs GROUP BY caller ORDER BY avg(prompt_len) DESC;

### 工具失败率
SELECT tool_name, error_type, count(*)
FROM steps WHERE status='failed'
GROUP BY tool_name, error_type ORDER BY count DESC;
```

### 示例 3：项目导航（auto_load: false）

```yaml
---
name: project-guide
description: "GDC_Bots 项目导航——文档层级、入口文件速查、requirements 用法"
auto_load: false
---

## 文档层级
L0: CLAUDE.md（每次必读）
L1: docs/DB_SCHEMA.md, docs/CONTEXT_LLM.md（按需）
L2: docs/REQ_HISTORY.md（理解演进）
L3: requirements/（非平凡任务必读）

## 入口速查
消息流：bridge/dispatcher/app.py
任务规划：bridge/dispatcher/planning/planner.py
Step 执行：bridge/dispatcher/execution/executor.py
Prompt 组装：bridge/context/assemblers/<scenario>.py
```

## 6. Agent 运行时行为

- **启动时**：自动扫描 `.harness/skills/` 目录
- **每个 cycle**：auto_load skills 注入 system prompt，其余显示为索引
- **运行中**：agent 可以用 `skill_lookup(name="xxx")` 加载任何 skill
- **写回**：agent 可以用 `skill_update(name, description, body)` 创建/更新 skill
- **无 skills 目录**：静默退回 mission 字段老逻辑，零影响
