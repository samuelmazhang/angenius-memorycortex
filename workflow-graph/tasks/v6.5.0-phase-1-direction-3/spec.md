# Task Spec — v6.5.0 Phase 1 direction #3 Nomic v1.5 embedding 升级

**Scenario**: new-feature
**Complexity**: M
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-3`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-3`
**mcp-graph node**: `node_58a0139f64b8` (status: backlog, daemon-side)
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 6 节

---

## 功能点 (主 arcadia 写,子代理实现)

必做 (按方案 v3 第 6 节):

1. **加 Nomic v1.5 依赖**
   - `plugin_package/memory_cortex/requirements.txt` 加 `nomic-ai/nomic-embed-text-v1.5`

2. **chunks 表加 model_version 字段**
   - `ALTER TABLE memory_chunks ADD COLUMN model_version VARCHAR DEFAULT 'mpnet';`
   - 新 chunks 写时 `model_version='nomic-v1.5'`
   - 老 chunks 保留 `'mpnet'`(增量 re-embed 时再更新)

3. **新 chunk 写入路径**
   - `server/src/embed_text()` 函数 (或等价) 检测 `os.getenv('EMBEDDING_MODEL', 'mpnet')`:
     - `'mpnet'` → 用现有 MiniLM-L12-v2 (向后兼容)
     - `'nomic-v1.5'` → 用新 nomic-embed-text-v1.5 (MTEB 62.28)
   - 写入时同步设置 `model_version` 字段

4. **query 路径切换**
   - `/memory/search` API 接受 `req.embedding_model` 字段 (默认 `nomic-v1.5`)
   - search 时根据 chunk 的 `model_version` 用对应 model embed(防止不同 model 嵌入维度混淆)

5. **background ETL re-embed**
   - 新 cron job `server/src/etl_reembed.py`:
     - 扫描 `model_version='mpnet'` 的 chunks
     - 用 nomic-v1.5 重 embed,更新 `model_version='nomic-v1.5'`
     - A/B grey 10% → 50% → 100%(默认 100% 启动, env override)
   - 不改 schema,只更新 embedding + model_version

## 验收 (主 arcadia 自动跑)

- pytest `server/tests/test_embedding_v6_5.py` 全过 (新建测试文件,5 个 test case):
  1. `test_requirements_txt_has_nomic_v1_5` — grep 验证 requirements.txt 含 `nomic-embed-text-v1.5`
  2. `test_chunks_table_has_model_version` — 通过 SQL 验证 `model_version` 字段存在 + DEFAULT 'mpnet'
  3. `test_embed_text_dispatches_to_nomic` — mock env var,验证 embed_text 调用 Nomic 而非 mpnet
  4. `test_search_uses_chunk_model_version` — 验证 query 时根据 chunk 的 model_version 选 model
  5. `test_etl_reembed_updates_chunks` — 验证 ETL 更新 model_version 字段
- 现有 test_taxonomy_boost.py / test_recall_v6_5.py / test_consolidate_v6_5.py 无回归
- `git diff --stat` 改动 ≤ 5 文件 + ≤ 400 行

## 不在 #3 范围

- LOCOMO harness (#4)
- HNSW m=24 (#5)
- consolidation conflict_resolve (#A.2) / scheduler (#A.3)
- graph index PPR (#B)
- summary compaction (#C)

## 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-3-report.md` (claude-code)
2. 主 arcadia 自动 commit + finish_task + 更新 mcp-graph

---

**注意**: spec 写功能点不写行数 (Pitfall O),所有行数 claude-code 自己决定。
