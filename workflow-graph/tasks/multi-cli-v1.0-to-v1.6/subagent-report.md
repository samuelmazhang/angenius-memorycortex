# multi-cli-graph-software-development v1.2 整合评审报告

> **状态**: DRAFT(整合,等你确认)
> **作者**: Arcadia Agent (主 arcadia 自己) 整合 claude / codex / opencode 3 份评审
> **时间**: 2026-08-22 23:35 CST
> **目标 SKILL**: `A:/Application/Arcadia-Agent/UserData/skills/software-development/multi-cli-graph-software-development/SKILL.md` (v1.2, 38.6KB / 536 行)

---

## 0. 评审元信息

| 来源 | 视角 | 网络搜索? | 外部引用数 | 完成 |
|---|---|---|---|---|
| **claude** | 工作流编排 + 多 CLI 协同 | 部分(spec-kit / squad / anthropic 2026 文档) | 3-4 | ✓ |
| **codex** | 工具集成 + 安全 | 是(Claude Code issues #3499 / #64346 / Stawinski RCE / h5i sandbox-exec 等) | 7+ | ✓ |
| **opencode** | 实践验证 + 真实案例 | 部分 | 1-2 | ✓ |
| **主 arcadia** | 覆盖度自动检查 + 自身反思 | 不需要 | 0(但跟 3 份独立评审交叉验证) | ✓ |

**整体一句话评估**(3 个评审 + 我自己交叉验证一致):
> v1.2 是**反思密度极高、验证密度为零**的 skill。5 铁律 + 12 红线 + 9 pitfalls + 7 步流程 + 9 问 checklist 看着滴水不漏,**但没跑过完整 sprint 验证**;真实场景覆盖不到 debug / monorepo / hotfix;可观测性 / 安全性 / 韧性(rollback/retry/circuit-breaker) 等生产级工程实践几乎缺失。

---

## 1. 严重问题(必须修,4 个评审者一致)

### 1.1 安全性严重不足(codex + 主 arcadia 一致)

**codex 视角**(详细):

| ID | 威胁 | 攻击场景 |
|---|---|---|
| M1 | prompt 注入 → report.md 把内部 token / IP / stack trace 回写主 arcadia | 子代理读到 `.env` 或 `failed to fetch http://internal:5432` 之类,带回主 arcadia |
| M2 | reviewer 自身可被污染 | `arcadia leaf reviewer` 读 report.md + diff 提 verdict,这俩都来自未受信子代理;verdict 文本若含 `<system>` 块污染主 arcadia 决策 |
| M3 | worktree 删除与子代理进程竞态 | unlink 后子代理句柄仍写 inode |
| M4 | `.env` 自动提交 | 子代理 `git add .` 会把 `.env` 拉进暂存 |

**外部参考**:
- Claude Code Concurrent sub-agents fail to spawn issue #3499
- Anthropic Claude Code Parallel tool calls issue #64346
- Stawinski RCE / GridTheGrey indirect prompt injection 案例
- h5i sandbox-exec profile 参考实现

**主 arcadia 补充**:
- v1.2 skill 内**无 secret / token redaction** 步骤(grep 验证 0 次)
- v1.2 skill 内**无 caller identity 透传**(unix socket peer creds / process pid → token)
- 铁律 2 "主 arcadia 独占 mcp-graph 写权限" 完全依赖 prompt 自我约束,**没有 daemon-side ACL enforce**

**修复建议**(合并 codex + 主 arcadia):
- mcp-graph 拆成 read/write 两套 server,主 arcadia 挂 write 端,子代理 CLI 配置只挂 read 端
- report.md 落主 arcadia 之前跑 redaction
- `.gitignore` 在 worktree 创建时**强制**写 `.sprint/` + `.env`
- worktree 销毁前:先 SIGTERM 子代理 + wait + 才 `git worktree remove --force`
- `.sprint/` 目录 `chmod 700`

### 1.2 可观测性接近为零(opencode + 主 arcadia 一致)

**opencode 视角**:
- 主 arcadia 看不到子代理实时状态,只能看**结果**;子代理跑到 80% 没 commit 时主 arcadia 一无所知
- 6 步验收任一红 → 整轮重派,**没有 fast path**
- mcp-graph `done_timestamp` 有,但 cycle time 实时性没暴露给 controller
- subagent 进程级 signal 完全没暴露

**修复建议**:
- 加 stream 机制 + progress 字段
- 子代理每 30 秒写心跳 JSON
- 失败诊断 fast path:`first_failure` 字段
- subagent metric 入 report.md

### 1.3 韧性工程实践全部缺失(主 arcadia 检查)

**v1.2 skill 完全没写**(grep 验证 0 次):

| 实践 | 缺失后果 |
|---|---|
| **rollback / snapshot 策略** | 子代理跑坏代码怎么回?GitHub Squad"每任务一 PR"自然回滚,v1.2 单 worktree 反而**缺这个机制** |
| **retry 重试策略** | 子代理 stall / network 抖动要不要 retry? |
| **circuit breaker** | 5 轮 fix loop 之后 escalate 到人工,v1.2 没说 |
| **cost / token 限制** | 3 个 CLI 跑久了烧 token 没保护 |
| **audit / 日志** | 出事了没法复盘"哪步谁跑的、跑了啥" |
| **merge conflict** | 并行 dispatch 多个 CLI 各自 commit,push 时冲突 |

**修复建议**:
- v1.3 加"韧性工程实践"章节(单独一节,5 条新红线)
- 每个 task `metadata` 加 `cost_budget_usd` / `timeout_minutes` / `max_retries` 字段
- 每个 task 完成后写 audit log JSONL

### 1.4 场景覆盖不全(opencode + 主 arcadia 一致)

**真实场景矩阵**(opencode):

| 场景 | skill 覆盖? | 现实障碍 |
|---|---|---|
| 单文件 bug fix | ❌ 没针对 | 启动前 9 问 = 杀鸡用牛刀 |
| 重构模块 | ⚠️ 勉强 | 粒度上限 5 文件 / 400 行 |
| **多语言 monorepo** | ❌ 没提 | `smoke_cmd` 强绑单语言测试命令 |
| **调试难 bug** | ❌ 完全没说 | debug 不是"加新功能",铁律 4 直接拒绝 |
| **安全 patch / CVE hotfix** | ❌ 紧急路径没有 | 启动前 9 问 + 7 步验收**最少 30 分钟 overhead** |

**修复建议**:
- 加 "场景快速路由" 章节,5 类场景各自走哪条路径
- `metadata.smoke_cmd` 改 array `["pytest server/", "npm test web/"]`
- 紧急 fast-path:hotfix 时跳过启动前 checklist 4-9

---

## 2. 中等问题(2 个评审者以上指出)

### 2.1 fix loop 引用而不实现(claude + opencode 一致)

**claude 视角**:v1.2 全文 38KB,"fix loop"只在末尾出现 1 次;superpowers 的 5 轮 fix loop 在 v1.2 里**没有具体步骤**,等于引用而不实现。

**opencode 视角**:铁律 5 引入新 stall 路径(3 个并行 + 都要等主 arcadia 评测试 = 串行瓶颈)。

**修复建议**:加 Fix Loop 实施细则(5 轮上限 + 每轮动作 + escalate)+ 铁律 5 异步评审

### 2.2 铁律 5 test_plan 没有模板(claude)

**修复建议**:v1.3 加 `references/test-plan-template.md`,给 5-10 个正例(Python / Node / .NET / Go / Rust / SQL)

### 2.3 3 CLI 选型决策树缺失(claude)

**修复建议**:加 CLI 选型决策表(任务类型 × 推荐 CLI)

### 2.4 铁律 4 "行数硬约束" 过度工程(claude)

**外部参考**:Spec-Kit / OpenSpec(按 acceptance criteria 切,不按行数)

**修复建议**:铁律 4 改为 "独立 acceptance criteria + 独立 commit + 可独立回滚",行数作为软建议

### 2.5 mcp-graph 健康降级路径缺失(claude)

**修复建议**:daemon 检测到不健康时立即降级到 `superpowers/subagent-driven-development`(纯文本 ledger + worktree)

### 2.6 批处理 dispatch 过度防御(claude)

**修复建议**:改 "批处理不接 stdin" 为 "checkpoint + 短轮 + exit-code 监控 + 中断恢复"

---

## 3. 现有铁律 / 红线 反驳

| 编号 | 反驳者 | 反驳 |
|---|---|---|
| **铁律 2** | codex | "主 arcadia 独占 graph 写" 是形式上的 — 无 daemon-side enforce |
| **P2** | codex | "文件不在 worktree 下视为越界" — prompt 而非 OS 约束 |
| **铁律 3** | codex | verdict 输入仍受 M2(reviewer 自身可被污染)威胁 |
| **铁律 4** | claude | 行数硬约束是过度工程;spec-first 文献主流按 acceptance criteria 切 |
| **铁律 5** | claude + opencode | 违反 spec-first 文献主流 + 引入新 stall 路径 |
| **P3** | opencode | 没说"同 model 2 个并行也算违反" |

---

## 4. 主 arcadia 额外发现

### 4.1 跟 mcp-graph-design 的关系不清

v1.2 SKILL.md 没引用刚建的 `mcp-graph-design` skill。读者不知道有 `mcp-graph-design` 存在,会重复造轮子。

**修复**:v1.3 在 "跟其它 skill 的关系" 表里加 `mcp-graph-design`

### 4.2 反思文档没 commit

v1.2 SKILL.md 引用 `docs/methodology/2026-08-22-multi-cli-incident-retrospective.md`,但这个文件还在 working tree,没 commit。引用飘文档 = 隐患。

**修复**:commit 反思文档(用户拍板后)

### 4.3 跟 v0.7.1 替代关系没彻底说清

v1.2 说"替换 v0.7.1",但 references 头部没标"从 v0.7.1 沿用"。

**修复**:references 头部加标注

---

## 5. 优先级矩阵

| 优先级 | 问题 | 修复耗时 |
|---|---|---|
| **P0(必修)** | §1.1 安全 | 2-3 天 |
| **P0(必修)** | §1.2 可观测性 | 2 天 |
| **P0(必修)** | §1.3 韧性(rollback + retry + circuit-breaker + audit) | 2 天 |
| **P1(应修)** | §1.4 场景覆盖 | 1-2 天 |
| **P1(应修)** | §2.1 fix loop 具体化 | 1 天 |
| **P2(可修)** | §2.2 test_plan 模板 | 0.5 天 |
| **P2(可修)** | §2.3 CLI 选型决策表 | 0.5 天 |
| **P2(可修)** | §2.4 铁律 4 改写 | 0.5 天 |
| **P3(可选)** | §2.5 mcp-graph 降级 + §2.6 批处理改 checkpoint | 各 0.5 天 |
| **P3(可选)** | §4.1-4.3 文档清理 | 0.5 天 |

**总估算**:P0 必修 6-7 天 → v1.3 / v2.0 重写。P1 应修 2-3 天。P2-P3 共 3-4 天。

---

## 6. v1.3 重写 plan(待你确认)

按 §5 优先级,我建议分两版:

- **v1.3 patch 版**(P0 必修):加 §1.1 / §1.2 / §1.3 三章,**保留现有 5 铁律 + 12 红线**,加 "安全 / 可观测 / 韧性" 三段
- **v2.0 重写版**(P1+P2):整合 §1.4 / §2.1-2.6 / §4,重写铁律 4 + 铁律 5,加场景快速路由,**破坏性变更**

我建议**先 v1.3**(更稳),跑一两次 sprint 后再 v2.0。

---

## 7. 一句话总结

> **3 个 CLI 评审 + 主 arcadia 自己 4 个维度交叉验证,得出统一结论**:v1.2 是"约束卡"级别的 skill(纸面滴水不漏),但缺工程韧性 / 安全 / 可观测 / 场景覆盖,**要变成真正可跑的方法论需要 v1.3 patch(补 P0 三章)→ 跑 1-2 sprint 验证 → v2.0 重写**。

---

## 8. 3 份评审 + 主 arcadia 补充的原始产出存档

- `claude-review.md`(1.8KB)
- `codex-review.md`(2.5KB)
- `opencode-review.md`(2.9KB)
- 主 arcadia 覆盖度自动检查结果(已在本报告 §1.3 / §4 综合)

路径:`..sprint-review/multi-cli-skill-v12-review/`