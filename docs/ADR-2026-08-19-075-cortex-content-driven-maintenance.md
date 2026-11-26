# ADR-075: Cortex 维护清理的内容驱动原则 ("old != should forget")

- Status: Accepted
- Date: 2026-08-19
- Author: 天行健 (<user>) + ExampleOrg Agent
- Supersedes: 无 (新决策，ADR-065/066 的时间驱动清理策略在此 ADR 下被显式约束)
- Depends on: ADR-068 (cortex-conflict-resolve V5.4) + V6.3.1 MCP 端点

## 背景 (Context)

cortex 当前提供的"维护清理"端点分两类范式:

| 范式 | 端点 | 行为 | 问题 |
|---|---|---|---|
| **时间驱动** | `cortex_ttl_archive(days=N)` | 30/90 天前 chunk 批量 archived=1 | 老 = 该忘 |
| | `cortex_forgetting_cleanup(force=True)` | expires_at < now 的 chunk 物理删除 | 老 = 该忘 |
| | `cortex_reconsolidate(memory_id=None)` | "Strengthen/weaken memories based on access + age" | age 维度让旧记忆变弱 |
| **内容驱动** | `cortex_conflict_scan` | 按 source_key + confidence + importance 桶化 supersede | 新信息覆盖老信息 |
| | `cortex_merge_duplicates(threshold=N)` | 向量相似度合并 | 内容字面重复 |
| | `cortex_compress_run` | 长 chunk 压缩 | 内容长度优化 |
| | `cortex_consolidate_run` | 抽取 facts 应用到 user core | 内容提炼 |
| | `cortex_rumination_run` | 聚类 + 矛盾检测 + 启发式 | 内容关联 |

时间驱动范式在 2026-08-19 第一次跑 weekly apply 时暴露问题:

```
cortex_ttl_archive(days=30) 会归栏 1892/2614 = 72% chunks
其中 4 月份的 BootstrapBlazor 文档 / Cron Job / arcadia 偏好
等用户明确表示"重要资料"会被一并归栏
```

用户原话 (2026-08-19 23:05):

> "不能因为一个信息久就选择遗忘它,是因为新的信息跟他有冲突才会忘记它,否则它不应该被遗忘,比如一些规则本身没变过也没有过时,不应该删掉它"

这条原则跟 cortex 现有时间驱动清理 (ADR-065 `cortex-forgetting-scheduler`, ADR-066 `cortex-forgetting-persistence`) 范式直接冲突。

## 决策 (Decision)

**所有 cortex 维护清理必须 content-driven，时间维度只能用于排序或展示，不能作为删除/归档的 trigger。**

### 核心原则

1. **`old` ≠ `should forget`**
   - 信息的"老"不是它该消失的理由
   - 信息该消失的唯一理由是 **被新信息覆盖** (content conflict)
   - 规则、习惯、参考文档等"长期不变"的信息应该**永久保留**

2. **时间维度的合法用途** (仍然允许)
   - 排序/展示: 召回时按 importance × recency 排序 (V6.x 已有)
   - 状态提示: 显示"这条记忆 X 天没访问"作为 UI 提示
   - 性能优化: 老数据的 tier 调整 (short_term → medium → long → permanent)
   - 报告: 统计"30 天没 update 的 chunk"作为健康指标

3. **时间维度的禁止用途**
   - 触发删除 (`cortex_forgetting_cleanup`)
   - 触发归档 (`cortex_ttl_archive`)
   - 触发弱化 (`cortex_reconsolidate` 的 age 维度)
   - 任何"X 天 → 物理动作" 的规则

### 实施变更

**代码层** (`server/src/sdk/cortex_client.py`, `server/src/mcp_server/cortex_mcp_server.py`):
- **保留**: conflict_scan / merge_duplicates / compress_run / consolidate_run / rumination_run (5 个内容驱动)
- **保留但仅用于统计**: cortex_forgetting_stats (返回 expired/expiring_soon 数字, 不触发任何动作)
- **保留但 dry-only**: cortex_ttl_archive / cortex_reconsolidate (weekly apply 中不调用, dry 模式可看 what-if)

**Cron 层** (`/opt/cortex-deploy/scripts/cortex_maint_weekly.py`):
- weekly-apply 现在只跑 5 个 content-driven 操作
- 3 个时间驱动 (ttl_archive / forgetting_cleanup / reconsolidate) 在 apply 中显式 SKIP, 永久保留在 dry 模式
- SKIP 理由永久写入 log (`SKIP (content-only policy) <tool_name>`)

**配置层** (`/opt/cortex-deploy/scripts/cortex_maint_weekly.py`):
- `--mode=apply` 任何未来添加的工具必须先通过 design review, 确认是 content-driven
- 如果需要"按 access 频率减弱" 类似逻辑, 用 `cortex_reconsolidate(memory_id=specific_id)` 单点调用, 不允许批量

### ADR 关系

| ADR | 关系 |
|---|---|
| ADR-068 (conflict-resolve) | **本 ADR 的依据** — 已经是 content-driven |
| ADR-065 (forgetting-scheduler) | **本 ADR 约束其扩展** — 调度器不应直接触发 destructive 操作 |
| ADR-066 (forgetting-persistence) | **本 ADR 约束其用途** — expires_at 字段保留作统计，但不再作为删除依据 |
| ADR-067 (heat-reinforce v6) | **不冲突** — heat 是 access-driven，跟 content-driven 不矛盾 |

## 影响 (Consequences)

### 正面

1. **数据保全**: 用户 4 月份的 BootstrapBlazor 文档 / Cron Job / arcadia 偏好等长期参考**不再被批量归档**
2. **预测性维护**: 维护行为**完全可预测** — 只有内容冲突才触发动作，age 维度不参与
3. **审计友好**: log 里的 supersede 决策**100% 是 content-driven**，可解释性极强
4. **避免误伤**: 之前 72% 一次性 archive 这种"宽幅"操作**不再可能**

### 负面 / 权衡

1. **存储增长**: 不按时间归档意味着 chunks 只增不减，长期可能积压 (mitigation: storage_health 监控 + 手动 storage_enforce)
2. **冲突检测依赖语义**: `cortex_conflict_scan` 基于 confidence + importance + source_key 桶化，**需要 cortex 写过 source_key 才能工作** (mitigation: cortex_conflict_upsert 强制 source_key)
3. **冷数据仍占空间**: 一年前没访问的 chunk 仍在 DB (可接受：cortex 设计目标不是 log compaction)

### 兼容性

- **向前兼容**: 用户/agent 调用 cortex 写入路径不变, 只是 weekly apply 行为改变
- **向后兼容**: 旧 `cortex_ttl_archive(days=N)` / `cortex_reconsolidate(memory_id=None)` 端点保留, 但 weekly cron 不再调用 (手动调用仍可)
- **config 迁移**: 之前的 maintenance config 不用改, 默认行为已经按本 ADR 收敛

## 验证 (Verification)

2026-08-19 23:05 第一次 weekly apply (content-only) 实测:
- `cortex_conflict_scan(apply, 7d)`: superseded=10, errors=0
- `cortex_merge_duplicates(th=0.95)`: 0 (保守阈值)
- `cortex_compress_run(max=50)`: compressed=8
- `cortex_rumination_run`: contradicted=200, merged=0 (信号, 不动)
- `cortex_consolidate_run`: noop (user_core 空, 后续抽 facts)
- 4 月份数据 100% 保留 (符合用户原则)
- total_memories: 2614 → 2614 (无删除)
- active: 2555 → 2544 (-11, 全部来自 conflict supersede, 不是 time-driven)
- total_relations: 924 → 986 (+62, 全部来自 rumination 创建)

## 实施时间线

| 时间 | 事件 |
|---|---|
| 2026-08-19 16:55 | f53a88c commit: 恢复 cortex_remember/recall alias user_id 透传 (commit hash) |
| 2026-08-19 17:00 | 8.2 deploy + 8/8 MCP 端到端验证 |
| 2026-08-19 17:30 | (a) batch retag: 2538 chunks → user_id=<user_id> |
| 2026-08-19 22:30 | (C) (D) 清理 28 e2e session_summary + 18 orphan core_profile |
| 2026-08-19 22:55 | 部署 daily + weekly cron, apply 默认 disabled |
| 2026-08-19 23:05 | weekly apply 第一次跑 (按 time-driven 30d), 发现会归栏 72% chunks |
| 2026-08-19 23:06 | **本 ADR 起草** — 用户原则 "old != should forget" 明确 |
| 2026-08-19 23:07 | weekly.py 改完 (移除 time-driven 3 ops) |
| 2026-08-19 23:08 | weekly apply 第二次跑 (content-driven), 4 月数据 100% 保留, 5/5 ops OK |

## 附: weekly.py 永久 SKIP 注释

```python
# REMOVED (time-driven, per ADR-075):
# - cortex_ttl_archive (days=N): batch archive by age — wrong paradigm
# - cortex_forgetting_cleanup (force=True): delete by expires_at — wrong paradigm
# - cortex_reconsolidate (memory_id=None): weaken by age — wrong paradigm
# Forgetting should be CONFLICT-DRIVEN, not time-driven.
```
