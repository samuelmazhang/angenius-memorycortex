# OpenCode 评审 — 实践验证 + 真实案例视角

## 真实场景覆盖矩阵
| 场景 | skill 覆盖? | 现实障碍? |
|------|-------------|----------|
| 单文件 bug fix | ❌ 完全没针对 | 启动前 9 问 + 7 步验收 = 杀鸡用牛刀 |
| 重构模块 | ⚠️ 勉强 | 粒度上限 5 文件 / 400 行,大重构要拆 20 个 task |
| 多语言 monorepo | ❌ 没提 | `smoke_cmd` 强绑单语言测试命令;monorepo 要 N 个 smoke 怎么跑? |
| 调试难 bug | ❌ 完全没说 | debug 不是"加新功能",没有 spec 也没有 test_plan,铁律 4 直接拒绝 |
| 安全 patch | ❌ 紧急路径没有 | 启动前 9 问 + 7 步验收最少 30 分钟 overhead;cve hotfix 场景完全失灵 |

## 失败场景对照(对比 v0.7.1)
- 失控场景 1: 无 worktree → v1.2 防:铁律 1 强制 `metadata.worktree` ✅
- 失控场景 2: 无任务图 → v1.2 防:铁律 2 + 启动前 #1 验 mcp-graph ✅
- 失控场景 3: 子代理伪造完成 → v1.2 防:铁律 3 亲自 ls ✅ 但**只防"完成 0 文件",不防"完成但跑 fail"**
- 失控场景 4: 并行争 quota → v1.2 防:红线 P3 限 3 个并行 ✅ 但**没说同 model 2 个并行也算违反**
- **v1.2 防不住的**:子代理 stall 在铁律 5 评测等待 — 3 个并行 + 都要等主 arcadia 评测试 = 串行瓶颈

## 可观测性缺口
- 进度盲区:主 arcadia 看不到子代理实时状态,只能看结果
- 失败诊断无 fast path:6 步验收任一红 → 整轮重派,没有"先单独验哪个最可能 fail 的子集"
- mcp-graph 没 stream:done_timestamp / in_progress_timestamp 有,但 cycle time 实时性没暴露给 controller
- subagent 自身进程级 signal 完全没暴露:CPU / memory / network stall 只能从"report.md 没出来"反推

## 反驳
- 铁律 5 是过度工程:主 arcadia 跟 implementer 知识背景接近时,test_plan 跟实现拆两轮 + 评测往返总耗时 > 主 arcadia 自己写测试
- 铁律 3 6 步验收太重:UI 改动跑 Playwright 必要,但纯后端重构每个 task 跑 6 步 = 70% overhead 在测试 setup 而非真验
- 应该直接 superpowers 做的场景:
  - 单 implementer + 单 reviewer 串行 → `subagent-driven-development` 够了,本 skill 是杀鸡用牛刀
  - 任务真独立但只 2-3 个 → `dispatching-parallel-agents` 已覆盖
  - 单文件 bug fix → `arcadia-claude-code-subagent-workflow` 一句话
- 没复刻 fix loop:v1.2 全文 38KB,"fix loop"只在末尾出现 1 次;superpowers 的 5 轮 fix loop 在本 skill 里**没有具体步骤**,等于引用而不实现

**一句话总结**:v1.2 是**反思密度极高、验证密度为零**的 skill。5 铁律 + 12 红线看着滴水不漏,但没跑过完整 sprint;铁律 5 引入新 stall 路径;真实场景覆盖不到 monorepo / debug / hotfix。可观测性几乎没有。fix loop 引用而不实现。**作为约束卡可用,作为跑得起来的方法论还差一次真实 dispatch**。