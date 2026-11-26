# Task Spec — v6.5.0 Phase 1 direction #A.2 ADR-068 conflict_resolve

**Scenario**: new-feature
**Complexity**: M
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-A2`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-A2`
**mcp-graph node**: `node_e05c670d7380`
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 5.2 节 Step 2

---

## 功能点

必做:

1. **新建 `server/src/consolidate/conflict_resolve.py`** (~250 行)
   - `resolve_conflicts(new_chunks: List[Chunk], db_query, db_execute) -> List[ConflictAction]`
   - 接受 extract() 输出 (含 facts + user_id metadata)
   - 按 user_id 分桶
   - 检测冲突:
     - **SUPERSEDE**: 新 chunk 跟老 chunk 同一 (entity, predicate), 但 value 矛盾 → 标老 chunk `archived=1`, 新 chunk `supersedes=<old_id>`
     - **DEDUPE**: 新 chunk 跟老 chunk 完全相同 (entity, predicate, value, user_id) → 跳过新 chunk
     - **KEEP**: 不冲突 → 写入新 chunk
   - 写 `memory_conflict_log` 表 (schema 已存在, 含 old_chunk_id / new_chunk_id / action / ts)

2. **改 `server/src/consolidate/__init__.py`** — export `resolve_conflicts` 函数

3. **新建 `server/src/consolidate/CHANGELOG.md`** (新建)
   - ADR-068 conflict_resolve 实现说明 + 测试结果

## 验收

- pytest `server/tests/test_conflict_resolve_v6_5.py` (新建, 5 个 test):
  1. `test_supersede_detects_conflicting_value` — 同 entity 不同 value 触发 SUPERSEDE
  2. `test_dedupe_detects_duplicate_fact` — 同 entity+value 触发 DEDUPE
  3. `test_keep_writes_new_chunk` — 不冲突时写入
  4. `test_conflict_log_records_action` — memory_conflict_log 表更新
  5. `test_resolve_conflicts_is_idempotent` — 重复跑安全
- 现有 84 tests 无回归

## 不在 #A.2 范围

- 实际调 LLM (#A.2 不调 LLM, 只接 extract() 输出)
- API endpoint 改造 (留 #A.4)
- scheduler 集成 (留 #A.3)

## 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-A2-report.md`
2. 主 arcadia 自动 commit + finish_task
