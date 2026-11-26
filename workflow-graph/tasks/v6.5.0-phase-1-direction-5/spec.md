# Task Spec — v6.5.0 Phase 1 direction #5 HNSW m=24

**Scenario**: single-file-bug-fix
**Complexity**: S
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-5`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-5`
**mcp-graph node**: `node_62068c377af1`
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 8 节

---

## 功能点 (主 arcadia 写,子代理实现)

必做:

1. **改 server/docker/unified/schema.sql**
   - 在 `CREATE INDEX memory_chunks_hnsw_idx ON memory_chunks USING hnsw (embedding vector_cosine_ops);` 这行后加注释说明 m=24, ef_construction=200

2. **加 server/src/migrations/2026_08_23_hnsw_m24.sql** (新建文件)
   ```sql
   -- V6.5.0 Phase 1 direction #5: HNSW m=16 → m=24 + ef_search=120
   ALTER INDEX memory_chunks_hnsw_idx SET (m = 24, ef_construction = 200);
   -- ef_search 走 session 变量, 不改 schema (per session 调优)
   ```

3. **加 server/src/config.py 的 HNSW 调优常量**
   - `HNSW_M = 24`
   - `HNSW_EF_SEARCH = 120` (默认, 可 env override)

4. **改 api_pg.py 启动时设置 `SET hnsw.ef_search = 120`** (如果当前没设)

## 验收 (主 arcadia 自动跑)

- pytest `server/tests/test_hnsw_v6_5.py` (新建, 3 个 test):
  1. `test_schema_has_hnsw_index_comment` — schema.sql 注释有 m=24 + ef_construction=200
  2. `test_migration_sets_m24` — migration SQL 含 `ALTER INDEX memory_chunks_hnsw_idx SET (m = 24, ...)`
  3. `test_config_has_hnsw_constants` — config.py 含 HNSW_M=24 + HNSW_EF_SEARCH=120
- 现有 test_taxonomy_boost / test_recall_v6_5 / test_consolidate_v6_5 / test_embedding_v6_5 无回归

## 不在 #5 范围

- Binary Quantization (用户禁用,方案 v3 已删)
- LOCOMO harness (#4)
- consolidation / graph / summary

## 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-5-report.md` (claude-code)
2. 主 arcadia 自动 commit + finish_task + 更新 mcp-graph
