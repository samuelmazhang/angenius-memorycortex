# ADR-068: Cortex 冲突解决 + 周期更新 (V5.4)

- Status: Accepted
- Date: 2026-08-05
- Author: MiniMax (Hermes Agent) + 用户联合决策
- Supersedes: 无 (新能力)
- Depends on: V5.3.9 schema + connector/hermes/scripts/cortex_maintenance.py

## 背景 (Context)

cortex 现有冲突处理能力分散,且不完整:

| 现有能力 | 行为 | 与用户期望差距 |
|---|---|---|
| `/merge-duplicates` | 向量相似度建链 | 只建 `similar` 关系,从不删除或覆盖 |
| `/consolidate/extract` | 按句号拆事实 | 无去重,无冲突检测,每调用 +1 行 |
| `/rumination/run` (ADR-067) | 矛盾打 metadata 标记 | 只打标不删,且只看 importance 差距不看 source_key |
| `OPS_COUNTERS["conflict_detected"]` | 计数器 | 不是真冲突检测 |
| `connector/hermes/cortex_maintenance.py` | hermes cron 调维护 endpoint | 没有 conflict-resolve 这一项 |

用户原话:
> "cortex 记忆体里新信息跟老信息冲突时,要优先更新老记忆,如果不能合并就删除旧的相关内容,添加新的内容,保证记忆最新且不冲突. 另外要有一个周期更新能力,就是根据新的决策优先旧的,如果确定是事实,新的事实要覆盖旧的内容."

## 决策 (Decision)

**新增 V5.4 conflict-resolve 子系统**,三层:

1. **write-path** (`/conflict/upsert`) — 写入新事实时自动解决冲突
2. **periodic** (`/conflict/scan`) — 周期任务扫所有有 source_key 的桶做兜底
3. **audit** (`/conflict/log`) — 运维查询 supersede 决策

### Schema 变更 (向后兼容)

`memory_chunks` 加 4 列 (默认 NULL / FALSE,旧记录零影响):
- `is_deprecated BOOLEAN DEFAULT FALSE`
- `deprecated_at TIMESTAMPTZ`
- `superseded_by TEXT`
- `metadata->>'source_key'` 用现有 jsonb metadata 列存,**不新增列**

新增 `memory_conflict_log` 表 (审计):

```
action ∈ {'supersede', 'mark_only', 'reject', 'error'}
decided_by ∈ {'auto', 'manual'}
```

### 决策矩阵

| 新事实 confidence | 新 importance vs 旧 importance | 决策 | 副作用 |
|---|---|---|---|
| >= 0.7 | >= | SUPERSEDE | 旧 chunk `is_deprecated=TRUE` + 写 history + 写 conflict_log |
| < 0.7 | 任意 | MARK_ONLY | 旧 chunk metadata 标 `challenged_by`,不删 |
| 任意 | 新 < 旧 | MARK_ONLY | 同上 |
| 任意 | 桶内无旧事实 | NOOP | 仅写入新事实 |

桶定义: `metadata->>'user_id' IS NOT DISTINCT FROM <user_id> AND metadata->>'source_key' = <source_key>`

### 周期任务

`cortex_maintenance.py daily` 新增两步 (位置在 `/reconsolidate` 之后):
- `POST /conflict/scan {lookback_days: 7, dry_run: false, max_buckets: 500}` — 兜底 reconciliation
- `GET /conflict/log?limit=50` — 写入 daily 报告

### 不做的事 (Out of Scope)

- **不引入 LLM 判断矛盾** — 决策靠 importance + confidence 两个标量,无 LLM 调用,延迟 < 50ms
- **不引入"合并文本"** — 用户原话"不能合并就删除旧的相关内容",决策是"supersede 或 mark_only",不做语义合并
- **不改 `/rumination/run`** — 它做的是无 source_key 维度的全局矛盾扫描,与 conflict-resolve 互补
- **不做版本回滚 UI** — superseded_by 字段已记录,可手工 `UPDATE ... SET is_deprecated=FALSE` 回滚

## 后果 (Consequences)

### 正面

- 用户原话描述的"新事实覆盖旧事实"行为有了正式端点
- 周期兜底修复异步竞态 / 手工 SQL 写入导致的多版本共存
- 全量审计,任何 supersede 决策可追溯
- 向后兼容:旧 chunk 没 source_key 不受影响,行为完全等价 V5.3.9
- 无新依赖 (只用了现有 jsonb + PG 基础能力)

### 负面 / 风险

- `metadata->>'source_key'` 用 jsonb 表达式索引,query planner 选择不一定最优;`/conflict/scan` 在大表 (百万 chunk) 时可能慢 → 已加 `lookback_days` + `max_buckets` 上限
- SUPERSEDE 决策不可逆 (除非手工 SQL) → 已写 conflict_log + memory_history,审计完整但回滚需人工
- decision 阈值 (`MIN_SUPERSEDE_CONFIDENCE = 0.7`) 是硬编码,如要改需改源码,后续可放 settings 表

## 实施 (Implementation)

代码改动:
- `server/docker/unified/schema.sql` — 加 4 列 + 新表 + 3 个新索引
- `server/src/api_pg.py` — 加 `_resolve_conflict_for_chunk` + `/conflict/upsert` + `/conflict/scan` + `/conflict/log` 共 4 个符号
- `connector/hermes/scripts/cortex_maintenance.py` — daily 任务流加 2 步

数据库迁移:
- entrypoint 自动应用 schema (CREATE TABLE IF NOT EXISTS / ALTER COLUMN 不存在需手动补)
- 旧库需手动补 4 列:
  ```sql
  ALTER TABLE memory_chunks
    ADD COLUMN IF NOT EXISTS is_deprecated BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS deprecated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_by TEXT;
  ```

部署顺序:
1. 部署 schema 变更 (PG 端)
2. 部署 api_pg.py 新版本
3. 部署 cortex_maintenance.py 新版本
4. 验证: `curl -X POST http://cortex/conflict/upsert -d '{...}'` 走通
5. 验证: daily cron 跑后 `/conflict/log` 有新记录

## 验证 (Verification)

- 单测: TBD (后续补 test_v54_conflict.py)
- 集成验证:
  ```
  curl -X POST $CORTEX/conflict/upsert -H 'Content-Type: application/json' -d '{
    "content": "QuantPlatform 项目部署在 127.0.0.1",
    "source_key": "quantplatform_deploy_host",
    "user_id": "u_001",
    "confidence": 0.95,
    "importance": 7
  }'
  # 第二次同样 source_key 但不同 content + confidence=0.9 → 应 supersede
  curl -X POST $CORTEX/conflict/upsert -H 'Content-Type: application/json' -d '{
    "content": "QuantPlatform 项目部署在 127.0.0.1 (已迁移)",
    "source_key": "quantplatform_deploy_host",
    "user_id": "u_001",
    "confidence": 0.9,
    "importance": 7
  }'
  # 查询审计
  curl "$CORTEX/conflict/log?user_id=u_001"
  ```

## 关联 (Related)

- ADR-067: 矛盾检测 (rumination) — 全局无 key 维度,本 ADR 是 source_key 维度的精确版
- ADR-065: Cortex 遗忘 scheduler SOP — 周期任务调度模式参考
- 用户偏好: "新决策优先旧决策,如果确定是事实,新的事实要覆盖就的内容" (2026-08-05)