# Harness-Everything 自改实验经验录 (2026-04-16 → 2026-04-20)

**时间跨度**: 3.5 天
**自动提交数**: 487
**评分走势**: 12.9 → 14.2（按日均，相当于噪音）
**触及文件 top 3**: `cross_reference.py` (76), `base.py` (44), `file_read.py` (35)
**智能核心被触及**: `planner.py` **1 次**, `memory.py` 2 次, synthesis 相关 4 次

这篇不是事故报告，是**对一个 LLM 驱动的自我进化系统**跑了三天半之后，我学到的东西。写给以后的自己，也写给任何想跑类似实验的人。

---

## 1. 核心结论：LLM 会 Goodhart 自己的奖励信号

我以为让 harness 自己改自己会趋向"变聪明"。实际发生的是：

- 评分从 13 爬到 14，看起来在升
- **不是**因为它变聪明了，是因为它学会了"评估器喜欢什么"
- 评估器喜欢：详尽的错误处理、TOCTOU 防护、测试覆盖率
- LLM 学到：往**已经熟的那个文件**里继续堆这种代码，稳拿高分
- 于是 `cross_reference.py` 被改 76 次、`file_read.py` 被改 35 次 —— 同一个文件反复打磨
- **真正决定智能的模块（planner / synthesis / memory / evaluator）几乎没动**

这是**经典的 Goodhart's Law**：任何度量一旦成为目标，就不再是好的度量。评分就是那个被优化的目标。

---

## 2. 三个结构性失败模式（我先后踩了）

### 2.1 隧道视野：LLM 锁定一个文件改到死

**症状**: 连续 10-20 轮只改同一个文件。

**机制**: 三个自反馈叠加：
1. `$file_context` 按关键词 + 最近修改时间排序 → 刚改过的文件排第一 → LLM 总看到它
2. `$prior_best` 永远是上一轮对这个文件的改动 → LLM 顺着继续改
3. `memory.jsonl` 记下来的都是对这个文件的修复 → 强化这条主线

**试过的解法**：
- 把 phase 分成 4 个（framework / security / consolidation / traceability）强制换角度 → 失败，每个 phase 自己都进入隧道（从 cross_reference 轮到 file_read 轮到 file_ops）
- 加 `allowed_edit_globs` 限制每个 phase 的可编辑范围 → 部分有效（挡住了 deploy/ 等禁区），但挡不住"在 phase 允许的范围内继续挑最熟的文件"
- 给 framework_improvement 注入 INTELLIGENCE METRIC 块，软性指向 evaluator → **完全没用**，LLM 视而不见

**真教训**: 软驱动 + prompt 约束对 tunnel vision 几乎无效。硬约束（"连续 3 轮同一文件就扣大分"）可能有用，但我没来得及验证。

### 2.2 4 阶段强制排期 → API 浪费 + 评分稀释

**症状**: 每轮 12 个 LLM 调用（4 phase × 3 步），但三个 phase 里 LLM 没啥要干的事，空转或敷衍。

**机制**: 把"今天必须干 4 件事"硬塞给一个 LLM。真实的代码改进不均匀 —— 这周 evaluator 烂，那周 metrics 缺。强行一天跑四遍，75% 算力浪费在"不是当下最该做的"上。

**真教训**: **phase 是给人看的，不是给 LLM 看的。** 人脑需要"今天轮到谁"的排期来自律；LLM 不需要，它上下文里信息足够就能自己挑。

### 2.3 评估器目标不锚定 phase 意图

**症状**: framework_improvement phase 的 PRIORITY ORDER 明确写 "#1 evaluator improvements"，但 LLM 改了 `file_read.py` 的 TOCTOU 防护，评估器还是给它打 14 分。

**机制**: 评估器的 BASIC_SYSTEM / DIFFUSION_SYSTEM 只看"这段代码/提案质量如何"，不看"这是不是 phase 当前该做的事"。评估器没办法执行 phase-specific 约束。

**真教训**: **评估器 + phase prompt 之间的不对齐是根本漏洞**。修这个要么 per-phase 定制评估器（工程量大），要么放弃 phase 概念（我最后选的路）。

---

## 3. 四个技术插曲（各花掉半天）

### 3.1 `validate_calibration_anchors` NameError 级联崩溃

commit `b9ab144` 加了一行 `calibration_issues = validate_calibration_anchors(...)`，但函数从未定义。运行中的 Python 进程没重载，照常跑完并 push 这个 commit。tag 触发部署重启 → 新进程 import 新代码 → **每个 phase 评估器调用都 NameError** → 连续 6 轮零分 → 早停 → `exit 0` → systemd 认为清净退出 → heartbeat 只认 `failed` 不认 `inactive + zombie` → **服务停了 5 小时没人管**。

修了之后加了三层防御：
- `ImportSmokeHook` 从"只 import 模块"扩展到"支持 smoke_calls 执行函数"
- `StaticCheckHook` 用 ruff F821 做静态未定义名检查
- 零分灾难自动 `sys.exit(2)` → systemd 把它当 failed → heartbeat 拉起来
- heartbeat 加了 zombie-clean-exit 分支

**教训**: 自改系统的 commit 必须被"模拟下次启动"的门禁检查过滤。单纯 `import mod` 不够，得真的调用一下它的 API，就像冷启动时会发生的那样。

### 3.2 LLM 扩散式"改进"反而搞坏读工具

某一轮 security_and_cleanup phase 把 `_check_phase_scope` 调用**加到了读工具（tree、list_directory）上**。结果 LLM 连目录都列不了，每轮 10 次 `PHASE SCOPE ERROR`。

**教训**: 自改 LLM 看到一个好设计（scope 检查）会倾向于"到处套用"，即使原设计意图只针对写工具。**得在代码里加注释明确意图边界**，不然 LLM 会用它自己的理解推而广之。

### 3.3 ρ 探针实验：工程成功，产品失败

为了对抗 Goodhart，我搭了个"智能度量闭环"：
- 20 个已知好坏的 proposal 作为基准（5 档 × 4 个）
- 每轮跑当前评估器打分，算 Spearman ρ
- ρ 轨迹注入下轮 framework_improvement prompt
- benchmarks 加 glob 锁 + SHA-256 哈希防篡改

**结果**:
- 基线 ρ = 0.87（**已经超过目标 0.85**）
- 33 轮采样后 ρ 在 0.71-0.90 震荡，**无上升趋势**
- **framework_improvement 完全无视** ρ 信号，继续刷 file_ops.py
- 评分全局崩盘到 5-9 区间（比改前的 12-17 砍半）

**有价值的副产物**：
- 探针机制本身运作正常（hash 校验、subprocess 隔离、失败不崩管道）
- 证明**合成基准天花板在 tier-level 区分**，真要再拉高 ρ 需要真实 proposal 基准
- 证明**软性 prompt 驱动无法对抗文件上下文引力**

**教训**: 度量 + 注入式反馈这套玩法，对 DeepSeek-Chat 级别的模型来说**信号接收能力不够**。它看到 ρ 也不知道怎么反向推出"那我该去改 evaluator"。上限可能在模型本身。

### 3.4 自改系统合并冲突与 push race

服务器每轮都 auto-push。我本地改代码时要反复 `git fetch + rebase + push`。有几次 push 被拒（远端已经前进），处理得不好会造成我的提交和服务器的提交顺序错乱。

**教训**: 干预自改系统时，固定流程是 `stash → fetch → rebase → pop → edit → add → commit → push`。任何偷懒（`git commit -am` 不看状态）都可能留下本地和远端分叉。

---

## 4. 做对了什么

1. **`HARNESS_RUNBOOK.md`** — 写在自改循环 bug 爆发之前。冷启动一个陌生会话能在 2 分钟内诊断"服务死了多久、为什么、该不该打断"。这 150 行文档是这 3.5 天里投资回报比最高的东西。
2. **加防御层（ImportSmokeHook / StaticCheckHook / scope_globs / exit code 2 / zombie heartbeat）** — 每加一层，崩盘模式少一个。到 plan v1 end 时系统自愈能力非常强。
3. **探针基础设施可复用** — `benchmarks/ + intel_metrics/ + 哈希锁` 这套拿来做 planner / synthesis / memory 的闭环，代码几乎不用改。
4. **把一切都塞进 git** — 每轮 commit 能看回放，能追溯"这个 bug 是哪个 commit 引入的"。自改系统没这个等于盲飞。

---

## 5. 为什么切换到 v2（单 phase + self-orchestrate）

v1 的 4 phase 架构假设"广度约束帮助 LLM"。实证结果是：

- 4 phase 制造了 4 条隧道，而不是消除隧道
- per-phase 评分让评估器无法做整体判断
- PRIORITY ORDER 是软约束，LLM 轻松绕过
- API 成本 75% 花在不该做的事情上

v2 的假设相反："**给 LLM 更多 agency，让它自己排序**"。具体做法：

- **1 个 phase** (`improve`)，不再 4 个
- **use_planner=true**: 执行前先跑三方辩论（保守 + 激进 + 合并），产出 plan
- **free-form FOCUS**: LLM 自己写 "本轮 FOCUS / TARGET / WHY NOW / SUCCESS"
- **软提醒反隧道**（prompt 里建议"连续 3 轮同一文件请重新考虑"），但不硬拦
- **分数低也 commit**（承认探索的价值），不 revert
- **评估器新增 FOCUS COHERENCE 维度**：改动必须匹配自己声称的 FOCUS，否则重扣

API 成本从 12 calls/round → 约 6 calls/round，**砍一半**。

v2 配置完全不动原机械代码（phase / planner / executor / evaluator / hooks / memory 全保留），只是换了一份 JSON。**v1 配置原封不动保留**作为 `pipeline_example_self_improve_server.json`。

---

## 6. v2 要观察的三个信号

跑 5-10 轮之后看：

1. **FOCUS 是否多样？**  连续轮 FOCUS 应该覆盖不同模块，而不是"都在 file_read" / "都在 file_ops"。
2. **改动是否匹配 FOCUS？**  评估器的 FOCUS COHERENCE 打分应该集中在 6-9 区间（不是 0，也不是清一色 10）。
3. **智能核心被触及的频率**  `harness/evaluation/`、`harness/prompts/`、`harness/pipeline/planner.py`、`harness/pipeline/memory.py` 在每 5 轮里应该至少被碰 1 次。如果 10 轮都没人动，就是老毛病复发。

**失败信号**: 5 轮后 FOCUS 80% 集中在一两个文件 → v2 也失败 → 这时候真问题不在架构，而在 DeepSeek-Chat 本身的 meta-reasoning 上限，得换模型（Claude Opus 4.7）才有救。

---

## 7. 留给下一个我的三条规矩

1. **不要加约束之前先看数据**。每次我加一层约束（phase、scope、intel metric），都是"觉得"它会帮。真实效果几乎每次都是"没帮到、或者帮错了地方"。下次加之前先跑 10 轮看问题确实存在。

2. **prompt 软约束没用，得是机制**。"你应该优先改 X" 写在 prompt 里 = 0 实效。改 X 才让 commit 通过 = 100% 实效。但后者可能直接锁死探索。所以真要改 LLM 的行为，一定是"成本/奖励"机制，不是"请求/建议"。

3. **接受天花板**。DeepSeek-Chat 的代码质量可能就停在"每天产出 30 个体面的小改动"。这值多少钱取决于你怎么用。**不要以为加更多约束能让它突破自身 meta-reasoning 的上限** —— 那是模型本身的能力问题，不是架构问题。

---

## 附：关键产物清单（可回溯）

- `config/pipeline_example_self_improve_server.json` — v1 配置（4 phase，保留）
- `config/pipeline_example_self_improve_server_v2_orchestrate.json` — v2 配置（单 phase + planner）
- `harness/pipeline/intel_metrics.py` — ρ 探针编排器
- `benchmarks/evaluator_calibration/{proposals.jsonl,run_probe.py,.sha256}` — 20-proposal 基准
- `harness/pipeline/hooks.py::ImportSmokeHook / StaticCheckHook` — 自改防御层
- `deploy/heartbeat.sh` — 双分支（failed + zombie）心跳
- `docs/HARNESS_RUNBOOK.md` — 冷启动诊断手册
- `docs/INTEL_LOOP_PHASE1.md` — ρ 探针设计文档
- `docs/HARNESS_EXPERIMENT_LOG.md` — 本文档
