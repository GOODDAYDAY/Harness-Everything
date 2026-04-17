# Harness-Everything 核心流程文档

## 全局视图

```mermaid
graph TB
    subgraph 本质
        A[LLM<br/>大语言模型] -->|输出 tool_use JSON| B[Harness<br/>你的 Python 代码]
        B -->|执行工具,返回结果| A
        B -->|read/write/edit| C[项目代码]
        C -->|注入上下文| A
    end

    style A fill:#4A90D9,color:white,stroke:none
    style B fill:#50C878,color:white,stroke:none
    style C fill:#FF8C42,color:white,stroke:none
```

> **LLM 是大脑,Harness 是手脚,项目代码是被改的对象。LLM 从来没有直接碰过文件系统——它只说"我想做什么",你的代码去执行。**

## 本质：三句话说清楚

1. **LLM 是发动机**：把项目代码扔给大语言模型,让它分析、提改进、写代码。核心就是一个 while 循环不停问 LLM"还能怎么改"。
2. **工具是手脚**：LLM 不能直接读写文件,所以通过 Anthropic 的 tool_use 协议告诉你的代码"我想读这个文件"/"我想改这行代码",你的代码去执行,再把结果喂回来。只留一个 `bash` 工具也能跑。
3. **重启是关键**：Python 模块加载后就固化在内存里。LLM 改了自己的 `.py` 文件,运行中的进程还是用旧代码。必须重启进程,改进才生效。所以有了 push → tag → CI 部署 → 重启 的闭环。

---

## 从最简单到完整系统：每一层解决一个问题

### 最简版本（概念上）

```python
while True:
    代码 = 读取项目所有文件()
    回复 = LLM("这是代码,帮我改进:" + 代码)
    写回文件(回复)
```

这就能跑。但会遇到一堆问题。**下面每一层都是为了解决上一层的问题**：

```mermaid
graph LR
    L0["while 循环<br/>能跑但粗糙"] --> P1["代码太多<br/>塞不进上下文"]
    P1 -->|加工具| L1["Tool Use<br/>按需读写"]
    L1 --> P2["LLM 会改出 bug"]
    P2 -->|加检查| L2["语法检查 Hook"]
    L2 --> P3["不知道改得好不好"]
    P3 -->|加评估| L3["双重评估器打分"]
    L3 --> P4["大改容易翻车"]
    P4 -->|分阶段| L4["Pipeline 多轮迭代"]
    L4 --> P5["改了代码不生效"]
    P5 -->|重启| L5["Tag → CI → 重启循环"]
    L5 --> P6["循环会断"]
    P6 -->|兜底| L6["心跳 + 回滚 + 强制 tag"]

    style P1 fill:#FF6B6B,color:white,stroke:none
    style P2 fill:#FF6B6B,color:white,stroke:none
    style P3 fill:#FF6B6B,color:white,stroke:none
    style P4 fill:#FF6B6B,color:white,stroke:none
    style P5 fill:#FF6B6B,color:white,stroke:none
    style P6 fill:#FF6B6B,color:white,stroke:none
    style L1 fill:#50C878,color:white,stroke:none
    style L2 fill:#50C878,color:white,stroke:none
    style L3 fill:#50C878,color:white,stroke:none
    style L4 fill:#50C878,color:white,stroke:none
    style L5 fill:#50C878,color:white,stroke:none
    style L6 fill:#50C878,color:white,stroke:none
```

### 第 1 层：代码太多,上下文放不下

**问题**：项目一大,所有文件塞不进 LLM 的上下文窗口。

**解法**：不再一次性塞所有代码,而是给 LLM 工具让它自己选择看什么。

```
LLM: "我想看 harness/core/llm.py"
      ↓
你的代码: open("harness/core/llm.py").read() → 返回文件内容
      ↓
LLM: "我想搜索哪些文件引用了 _check_path"
      ↓  
你的代码: grep -r "_check_path" → 返回搜索结果
      ↓
LLM: "我要改这个文件的第 285 行"
      ↓
你的代码: 执行修改
```

这就是 **tool_use 机制** —— LLM 输出一段 JSON 说"我想调什么工具、传什么参数",你的 Python 代码去执行,把结果喂回来。LLM 从来没有直接碰过文件系统。

```mermaid
sequenceDiagram
    participant LLM as LLM (DeepSeek)
    participant H as Harness (你的代码)
    participant FS as 文件系统

    LLM->>H: tool_use: read_file("llm.py")
    H->>FS: open("llm.py").read()
    FS-->>H: 文件内容
    H-->>LLM: 工具结果: "class LLM:..."
    
    LLM->>H: tool_use: grep_search("_check_path")
    H->>FS: grep -r "_check_path"
    FS-->>H: 搜索结果
    H-->>LLM: 工具结果: "base.py:285..."
    
    LLM->>H: tool_use: edit_file("base.py", ...)
    H->>FS: 修改文件
    FS-->>H: OK
    H-->>LLM: 工具结果: "已修改"
    
    LLM-->>H: (无 tool_call) "我改完了"
    Note over H: 循环结束,返回最终文本
```

**核心代码（`harness/core/llm.py` 的 `call_with_tools()`）只有 60 行**：

```python
for turn in range(max_turns):
    # 1. 问 LLM:"你想做什么?"
    response = await api.messages.create(messages=对话, tools=工具列表)
    
    # 2. 如果 LLM 没有要调工具 → 它说完了,退出
    if not response.tool_calls:
        return response.text
    
    # 3. LLM 要调工具 → 你的代码执行 → 把结果喂回去
    for call in response.tool_calls:
        result = await registry.execute(call.name, config, call.input)
    
    # 4. 结果追加到对话历史,继续循环
    对话.append(tool_results)
```

**就这些。这是整个项目最核心的 60 行。**

### 第 2 层：LLM 会改出 bug

**问题**：LLM 改的代码可能有语法错误,甚至会搞坏项目。

**解法**：每次改完代码,自动跑 `python -m py_compile` 检查语法。如果出错,把错误信息告诉 LLM,让它修。

```
LLM 改了代码 → py_compile 报错 → 错误信息喂回 LLM → LLM 再改 → 通过
```

这就是 `harness/pipeline/hooks.py` 里的 `SyntaxCheckHook`。

### 第 3 层：不知道改得好不好

**问题**：LLM 改了一轮,改得好吗?怎么判断?

**解法**：用另一次 LLM 调用来评判。而且用两个独立的评判者（互相看不到对方的评分），从不同角度打分：

- **Basic evaluator**：找最关键的缺陷（安全漏洞、逻辑错误、代码质量）
- **Diffusion evaluator**：分析二阶效应（会不会破坏其他模块？会不会让上下文膨胀？）

```
LLM 改了代码
  ├── Basic evaluator:    "这个改动有安全问题" → 5 分
  └── Diffusion evaluator: "但不会影响其他模块" → 7 分
  
合计 12 分。跟其他方案比,选最高分的。
```

```mermaid
graph LR
    subgraph 评估过程
        CODE[LLM 改了代码] --> B[Basic Evaluator<br/>找缺陷 → 5分]
        CODE --> D[Diffusion Evaluator<br/>看波及 → 7分]
        B --> SUM[合计 12 分]
        D --> SUM
    end
    
    subgraph 选择
        SUM --> CMP{跟其他方案比}
        CMP -->|最高分| WIN[采纳]
        CMP -->|不是最高| LOSE[淘汰]
    end

    style B fill:#E74C3C,color:white,stroke:none
    style D fill:#3498DB,color:white,stroke:none
    style WIN fill:#50C878,color:white,stroke:none
    style LOSE fill:#95A5A6,color:white,stroke:none
```

这就是 `harness/evaluation/dual_evaluator.py`。本质上是让 LLM 自己评自己,但通过**隔离两个评估视角**来减少自我吹嘘。

### 第 4 层：一次改一点,多轮迭代

**问题**：一次性让 LLM "把所有问题都修了" 效果很差。大改容易翻车。

**解法**：拆成多个阶段（phase），每个阶段专注一件事,一轮一轮迭代：

```
Outer Round 1:
  Phase 1: 分析代码,找问题（debate 模式,只看不改）
  Phase 2: 改进核心框架（implement 模式,用工具改文件）
  Phase 3: 安全加固 + 删死代码
  Phase 4: 整合工具 + 加测试
  Phase 5: 改善可追踪性

Outer Round 2:
  ...重复,但基于 Round 1 的改进继续
```

每个阶段内部还有**内层轮次**（inner rounds）：生成多个方案,评估器选最好的,然后合成。

```mermaid
graph TD
    R1[Outer Round 1] --> P1[Phase 1: 分析<br/>debate 只看不改]
    P1 -->|合成结论| P2[Phase 2: 改进<br/>implement 改文件]
    P2 -->|commit| P3[Phase 3: 安全<br/>implement 删死代码]
    P3 -->|commit| P4[Phase 4: 整合<br/>implement 合并工具]
    P4 -->|commit| P5[Phase 5: 追踪<br/>implement 加指标]
    P5 --> PUSH1[git push]
    PUSH1 --> PAT{patience<br/>有进步?}
    PAT -->|有| R2[Outer Round 2<br/>基于改进后的代码继续]
    PAT -->|连续5轮没有| STOP[早停退出]
    R2 --> P1_2[Phase 1...] --> P2_2[Phase 2...] --> R3[...]

    subgraph 每个 Phase 内部
        I1[Inner Round 1<br/>方案 A] --> EVAL[评估器打分]
        I2[Inner Round 2<br/>方案 B] --> EVAL
        EVAL --> SYNTH[合成最佳方案]
    end

    style R1 fill:#4A90D9,color:white,stroke:none
    style R2 fill:#4A90D9,color:white,stroke:none
    style STOP fill:#FF6B6B,color:white,stroke:none
    style SYNTH fill:#50C878,color:white,stroke:none
```

这就是 `harness/pipeline/pipeline_loop.py`（外层）和 `harness/pipeline/phase_runner.py`（阶段执行）。

### 第 5 层：改了不生效

**问题**：LLM 在 Round 3 改了 `harness/core/llm.py`,但运行中的进程还在用 Round 1 加载的旧代码。改了等于没改。

**解法**：你设计的重启闭环。

```
服务器跑 10 轮 → git commit + push → 打 tag → push tag
                                                 ↓
GitHub Actions 看到 tag → SSH 到服务器 → 部署新代码 → 重启进程
                                                         ↓
                                              新代码生效,再跑 10 轮...
```

**这是你做的最核心的架构决策。** 没有这个循环,自我优化就是假的——LLM 以为自己改了代码,但改动永远不会执行。

### 第 6 层：循环别断了

**问题**：各种情况会让循环意外停止。

| 问题 | 解法 |
|---|---|
| 代码太多上下文爆了 | 给 LLM 工具,让它按需读 |
| LLM 改出 bug | 语法检查 hook 自动验证 |
| 不知道改得好不好 | 双重评估器打分,选最高的 |
| 改了不生效 | push → tag → CI → 重启进程 |
| 连续几轮没进步 → 早停 → 没打 tag | `auto_tag_at_end` 强制每次退出都打 tag |
| 崩溃 3 次 systemd 放弃 | 心跳 cron 每 30 分钟检查并重启 |
| 用户推 commit 导致 push 冲突 | `git pull --rebase` 自动合并 |
| LLM 把部署脚本改坏了 | prompt 里的 PROTECTION 黑名单 |
| 部署了坏代码 | CI 烟测 + 回滚到 harness-last-good |
| 磁盘满 | 清理 cron 每天删旧数据 |

---

## 工具系统：不是核心,是优化

如果只有一个 `bash` 工具,LLM 会这么干：

```
bash("cat harness/core/llm.py")           → 读文件
bash("grep -rn '_check_path' harness/")   → 搜索
bash("sed -i 's/old/new/' file.py")       → 改文件
bash("python3 -m py_compile file.py")     → 检查语法
```

完全能跑。那为什么还搞 30 个专用工具？

| 用 bash | 用专用工具 | 为什么换 |
|---|---|---|
| `cat /etc/passwd` | `read_file` 会拒绝 | **安全**：路径检查限制在 workspace 内 |
| `grep` 输出 10 万行 | `grep_search` 自动截断 | **省 token**：不会撑爆上下文 |
| `sed` 改错了没法回退 | `edit_file` 精确替换 | **可控**：替换失败会报错,不会静默改坏 |
| LLM 编造参数 | registry 参数校验 | **容错**：未知参数直接拦截 |

```mermaid
graph TD
    subgraph "只用 bash（能跑,但危险）"
        LLM1[LLM] -->|bash cat /etc/passwd| BASH[bash 工具]
        BASH -->|无限制执行| FS1[文件系统]
    end

    subgraph "用专用工具（安全,省钱）"
        LLM2[LLM] -->|read_file /etc/passwd| CHECK[_check_path 检查]
        CHECK -->|路径越界!| REJECT[❌ 拒绝]
        LLM2 -->|read_file main.py| CHECK2[_check_path 检查]
        CHECK2 -->|在 workspace 内| ALLOW[✅ 执行]
    end

    style REJECT fill:#FF6B6B,color:white,stroke:none
    style ALLOW fill:#50C878,color:white,stroke:none
    style BASH fill:#FF8C42,color:white,stroke:none
```

**工具是给 LLM 戴的安全手套,不是给它的超能力。**

### 30 个工具分类

```
文件操作:    read_file, write_file, edit_file, delete_file, move_file, copy_file
目录操作:    list_directory, create_directory, tree
搜索:       glob_search, grep_search
Git:        git_status, git_diff, git_log
执行:       bash, python_eval, test_runner
分析:       code_analysis, symbol_extractor, cross_reference, ...
可选:       web_search (需要显式开启)
```

### 工具安全边界（`_check_path`）

每个文件操作工具在执行前都要过安全检查：

```python
resolved = os.path.realpath(path)  # 解析符号链接
# 检查 null bytes、Unicode 伪装字符、路径越界
if path 不在 allowed_paths 内:
    拒绝执行
```

LLM 说"读 /etc/passwd" → 被拦截。说"读 ../../etc/passwd" → realpath 解析后还是被拦截。

---

## 工具循环的成本模型

Anthropic API 是**无状态**的。每次调用都要重发完整对话历史。所以工具循环的每一轮都比上一轮贵：

```
Turn  1: 发送 [系统提示 + 文件上下文 + 用户指令]       ≈ 20K tokens
Turn  5: 发送 [上面 + 4 轮工具调用和结果]               ≈ 30K tokens
Turn 10: 发送 [上面 + 9 轮]                             ≈ 40K tokens
Turn 20: 发送 [上面 + 19 轮]                            ≈ 60K tokens  ← 最贵

一个 20-turn 循环总共: ≈ 0.5M input tokens
```

```mermaid
graph LR
    subgraph 每轮重发全部对话
        T1["Turn 1<br/>20K tokens<br/>$$$"] --> T5["Turn 5<br/>30K tokens<br/>$$$$"]
        T5 --> T10["Turn 10<br/>40K tokens<br/>$$$$$"]
        T10 --> T15["Turn 15<br/>45K tokens<br/>$$$$$$"]
        T15 --> T20["Turn 20<br/>60K tokens<br/>$$$$$$$$"]
    end

    T15 -.- CUT["✂️ 砍到这里<br/>省 40% 成本"]

    style T1 fill:#50C878,color:white,stroke:none
    style T5 fill:#8BC34A,color:white,stroke:none
    style T10 fill:#FF8C42,color:white,stroke:none
    style T15 fill:#FF6B6B,color:white,stroke:none
    style T20 fill:#D32F2F,color:white,stroke:none
    style CUT fill:#FFD700,color:black,stroke:#D32F2F,stroke-width:3px
```

**后半程每多一轮,花的钱比前半程多得多。** 这就是为什么 `max_tool_turns` 从 30 砍到 20 能省 40% —— 砍掉的是最贵的那几轮。

### 缓解措施

| 机制 | 文件 | 原理 |
|---|---|---|
| 对话剪枝 | `llm.py` | 总字符超 150K 时,截断旧的工具结果 |
| 主动压缩 | `llm.py` | turn >= 6 后,旧工具结果替换为一行摘要 |
| 文件读取缓存 | `llm.py` | 同一循环内 read_file 结果缓存,写操作使缓存失效 |
| 代码注入预算 | `phase_runner.py` | 只注入最相关的 30K 字符源码,不是全部 |

### DeepSeek 成本估算

```
Cache miss: $0.28 / 百万 input tokens
Cache hit:  $0.028 / 百万 input tokens (缓存命中率约 90%)
Output:     $0.42 / 百万 output tokens

一个 chunk (6-10 轮 × 4-5 阶段):
  Input:  ~30M tokens → ~$3
  Output: ~1M tokens  → ~$0.4
  合计:   ~$3.5 / chunk
```

---

## Pipeline 架构详解

### 整体结构

```
PipelineLoop.run()
│
├── for outer in range(10):                    ← 外层: 10 轮
│   │
│   ├── _run_outer_round()
│   │   │
│   │   └── for phase in [分析, 改进, 安全, 整合, 追踪]:  ← 5 个阶段
│   │       │
│   │       └── PhaseRunner.run_phase()
│   │           │
│   │           ├── 注入代码上下文 (最相关的 30K 字符)
│   │           │
│   │           ├── for inner in range(2):       ← 2 次尝试
│   │           │   │
│   │           │   ├── debate 模式: 并行,只分析不改
│   │           │   └── implement 模式: 顺序,用工具改文件
│   │           │       └── call_with_tools()    ← 工具循环 (最多 20 轮)
│   │           │
│   │           ├── DualEvaluator.evaluate()      ← 双重评估,选最好的
│   │           ├── Synthesis                     ← 合成最佳方案
│   │           └── Hooks: 语法检查 + git commit  ← 验证 + 提交
│   │
│   ├── auto_push (git pull --rebase + push)     ← 每轮推到 GitHub
│   ├── patience 检查 (连续 5 轮没进步 → 早停)
│   └── 优雅关闭检查 (收到 SIGTERM → 干完当前阶段退出)
│
├── auto_tag_at_end → 打 tag + push tag          ← 触发 CI 部署
└── 写 summary.json
```

### 每一层做什么

**外层轮次** (`pipeline_loop.py`):
- 编排所有阶段的执行顺序
- 跟踪分数趋势（3 连降警告）
- 决定是否早停（patience）
- 决定是否推送（auto_push）
- 决定是否打 tag（auto_tag）

**阶段执行** (`phase_runner.py`):
- 按关键词 + 修改时间 + 文件大小排序,注入最相关的源码
- debate 模式：LLM 只分析不改,并行跑多个方案,快
- implement 模式：LLM 用工具改文件,顺序跑（因为会改文件,不能并行）
- 评估：两个独立 LLM 调用并行打分
- 合成：从多个方案中提取最佳元素
- 验证：语法检查 + git commit

**工具循环** (`llm.py`):
- LLM 说"我要调这个工具" → 你的代码执行 → 结果喂回来 → 循环
- 每轮都重发完整对话（API 无状态）
- 自动剪枝防止上下文爆炸
- 最多 20 轮（成本控制）

### 关键数据流

```
prior_best (上一轮最佳方案)
    ↓
Phase 1 (分析) → synthesis → 传给 Phase 2
    ↓
Phase 2 (改进) → 改文件 → commit → synthesis → 传给 Phase 3
    ↓
Phase 3 (安全) → 改文件 → commit → synthesis → 传给 Phase 4
    ↓
...每个 phase 的合成结论传给下一个 phase 作为 prior_best
    ↓
Round 结束 → 最终 prior_best 传给下一个 Round
```

---

## 自改进循环（服务器部署）

### 架构图

```mermaid
graph TD
    subgraph 服务器
        S1[systemd 启动<br/>python main.py] --> S2[跑 10 轮<br/>每轮 commit + push]
        S2 --> S3[退出<br/>打 tag + push tag]
    end

    S3 -->|tag 推到 GitHub| CI

    subgraph CI[GitHub Actions]
        C1[SSH 到服务器] --> C2[git fetch + reset]
        C2 --> C3[cp 配置模板]
        C3 --> C4{py_compile<br/>烟测}
        C4 -->|通过| C5[设 harness-last-good]
        C4 -->|失败| C6[回滚到<br/>harness-last-good]
        C5 --> C7{STOP_AFTER_CHUNK<br/>标记存在?}
        C6 --> C7
        C7 -->|没有| C8[重启服务]
        C7 -->|有| C9[不重启<br/>循环暂停]
    end

    C8 -->|新代码生效| S1

    style S1 fill:#4A90D9,color:white,stroke:none
    style S3 fill:#FF8C42,color:white,stroke:none
    style C5 fill:#50C878,color:white,stroke:none
    style C6 fill:#FF6B6B,color:white,stroke:none
    style C8 fill:#50C878,color:white,stroke:none
    style C9 fill:#95A5A6,color:white,stroke:none
```

### 为什么需要重启

```python
# 进程启动时
import harness.core.llm  # 加载到内存,之后不再变

# Round 5: LLM 改了磁盘上的 llm.py
edit_file("harness/core/llm.py", ...)  # 磁盘变了

# Round 6: 进程还是用内存里的旧版本
# Python 不会自动重新 import

# 只有重启进程,新代码才生效
```

### 操作手册

| 想做什么 | 怎么做 |
|---|---|
| 看实时日志 | `ssh server "tail -f ~/harness-everything/logs/harness.log"` |
| 看 commit 进度 | `git log --oneline -20` |
| 推一个修复（不停服务） | 直接 `git push`,harness 会 `pull --rebase` 自动合并 |
| 改配置 | 改 `config/pipeline_example_self_improve_server.json`,push,下次部署自动生效 |
| 跑完这轮就停 | `ssh server "touch ~/.config/harness/STOP_AFTER_CHUNK"` |
| 恢复循环 | `ssh server "systemctl --user start harness.service"` |
| 立刻停 | `ssh server "systemctl --user stop harness.service"` |
| 彻底关闭 | stop + disable + 清 cron |

---

## 核心数据结构

```
PipelineConfig                     # 顶层配置
├── harness: HarnessConfig         #   模型/API/workspace/工具
│   ├── model: "deepseek-chat"
│   ├── base_url: "https://api.deepseek.com/anthropic"
│   ├── workspace: "/home/ubuntu/harness-everything"
│   ├── allowed_paths: [workspace]
│   └── max_tool_turns: 20
├── phases: [PhaseConfig]          #   阶段列表
│   ├── name, mode (debate/implement)
│   ├── system_prompt (含 $file_context 等模板变量)
│   └── glob_patterns (注入哪些文件)
├── outer_rounds: 10               #   每个 chunk 跑几轮
├── patience: 5                    #   几轮没进步就早停
├── auto_push_interval: 1          #   每轮 push
└── auto_tag_at_end: true          #   退出必打 tag

InnerResult                        # 单次尝试的结果
├── proposal: str                  #   LLM 的提案或改动
├── dual_score                     #   双重评分
│   ├── basic: (score, critique)   #     缺陷评估
│   └── diffusion: (score, critique)#    波及效应
└── tool_call_log: [dict]          #   工具调用记录

PhaseResult                        # 阶段结果
├── synthesis: str                 #   合成后的最终方案
├── best_score: float              #   最高分
└── inner_results: [InnerResult]   #   所有尝试
```

---

## 关键文件索引

| 文件 | 核心职责 | 一句话 |
|---|---|---|
| `main.py` | 入口 | 解析参数,启动循环 |
| `harness/core/llm.py` | **最核心** | 工具循环:LLM 说 → 你执行 → 反馈 → 重复 |
| `harness/core/config.py` | 配置 | JSON → 配置对象,路径安全验证 |
| `harness/pipeline/pipeline_loop.py` | 外层循环 | 轮次编排、push、tag、早停、关闭 |
| `harness/pipeline/phase_runner.py` | 阶段执行 | 代码注入、内层轮次、评估、合成、hooks |
| `harness/evaluation/dual_evaluator.py` | 质量把关 | 两个 LLM 并行打分,选最好的方案 |
| `harness/tools/registry.py` | 工具分发 | 工具注册、参数校验、异常封装 |
| `harness/tools/base.py` | 工具安全 | `_check_path` 路径边界检查 |
| `harness/pipeline/hooks.py` | 验证 | 语法检查 + git commit（富信息） |
| `deploy/harness.service` | 部署 | systemd 服务定义 |
| `.github/workflows/deploy.yml` | CI/CD | tag 触发 → 烟测 → 部署 → 重启/回滚 |
| `deploy/heartbeat.sh` | 保活 | 崩溃后自动重启 |

---

## 完整数据流：从 JSON 配置到代码提交

```mermaid
flowchart TD
    CONFIG["pipeline_server.json<br/>配置文件"] --> PARSE["PipelineConfig.from_dict()<br/>解析配置,过滤注释"]
    PARSE --> INIT["PipelineLoop.__init__()<br/>创建 LLM / Registry / Artifacts"]
    INIT --> OUTER["外层循环 (10 轮)"]

    OUTER --> INJECT["注入代码上下文<br/>glob 匹配 → 关键词排序 → 截取 30K"]
    INJECT --> MODE{debate or<br/>implement?}

    MODE -->|debate| DEBATE["LLM.call()<br/>纯文本分析,不改文件<br/>并行跑 2 个方案"]
    MODE -->|implement| IMPL["LLM.call_with_tools()<br/>工具循环 (最多 20 轮)<br/>读/搜/改文件"]

    DEBATE --> EVAL["DualEvaluator.evaluate()<br/>Basic + Diffusion 并行打分"]
    IMPL --> EVAL

    EVAL --> SYNTH["Synthesis<br/>合成最佳方案"]
    SYNTH --> HOOKS["Hooks<br/>py_compile 检查<br/>git commit (富信息)"]

    HOOKS --> PUSH["git pull --rebase<br/>git push"]
    PUSH --> PATIENCE{patience<br/>检查}
    PATIENCE -->|有进步| OUTER
    PATIENCE -->|5 轮没进步| TAG

    OUTER -->|10 轮跑完| TAG["auto_tag_at_end<br/>打 tag + push tag"]
    TAG --> CI["GitHub Actions<br/>烟测 → 部署 → 重启"]
    CI -->|新进程| INIT

    style CONFIG fill:#FFD700,color:black,stroke:none
    style IMPL fill:#4A90D9,color:white,stroke:none
    style EVAL fill:#E74C3C,color:white,stroke:none
    style SYNTH fill:#50C878,color:white,stroke:none
    style TAG fill:#FF8C42,color:white,stroke:none
    style CI fill:#9B59B6,color:white,stroke:none
```

---

## 一段话总结

> 把项目代码扔给 LLM,让它分析、提改进方案、用工具改代码。用另一个 LLM 调用评判改得好不好,选最好的方案 commit。多轮迭代,每轮都比上一轮基于更好的代码。因为 Python 模块加载后就固化在内存里,所以每 10 轮重启一次进程让改进生效。重启通过 git tag 触发 GitHub Actions 自动部署实现,形成无人值守的自改进循环。工具系统（30 个文件/搜索/执行工具）本质上只是给 LLM 戴的安全手套——只留一个 bash 也能跑,但更危险、更费 token。
