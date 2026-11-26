# Dispatch — v6.5.0 Phase 1 direction #3 Nomic v1.5

**收件方**: claude-code (subagent)
**发件方**: 主 hermes (auto-pilot mode)
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-3`
**mcp-graph node**: `node_58a0139f64b8`

---

## 任务

实现 #3 Nomic v1.5 embedding 升级 + 增量 re-embed (3.5 人天 spec)。

## 必读

1. `spec.md` (同目录) — 完整功能点 + 验收
2. `docs/改进方案-cortex-v6.5.0-完整版-20260822.md` 第 6 节 — 原始 spec
3. 现有 `plugin_package/memory_cortex/requirements.txt` — 加 nomic-ai 依赖
4. `server/src/api_pg.py` 第 7 节 + L256 等 embed_text 用法
5. `server/src/memory_taxonomy.py` (if applicable) — taxonomy boost 兼容

## 必做 (5 步,见 spec.md 详情)

1. 加 `nomic-ai/nomic-embed-text-v1.5` 到 requirements.txt
2. ALTER TABLE memory_chunks ADD COLUMN model_version VARCHAR DEFAULT 'mpnet'
3. embed_text() 函数加 dispatch (env var 选 model, 写 model_version 字段)
4. /memory/search 接受 embedding_model 字段 (default nomic-v1.5), 根据 chunk 的 model_version 选 model
5. background ETL: server/src/etl_reembed.py 重 embed 老 chunks

## 必测 (新建 server/tests/test_embedding_v6_5.py, 5 个 test)

1. `test_requirements_txt_has_nomic_v1_5`
2. `test_chunks_table_has_model_version`
3. `test_embed_text_dispatches_to_nomic`
4. `test_search_uses_chunk_model_version`
5. `test_etl_reembed_updates_chunks`

## 完成品

写 `<worktree>/.sprint/v6.5.0-phase-1-direction-3-report.md`:

```markdown
# #3 Nomic v1.5 Report

## Summary
- requirements.txt 添加 (粘贴 diff)
- ALTER TABLE migration (粘贴 SQL)
- embed_text() dispatch 实现 (粘贴关键函数)
- ETL job 实现 (粘贴入口)

## Tests
- pytest test_embedding_v6_5.py 全过
- 现有 tests/ 无回归

## Files Changed
- requirements.txt / api_pg.py / etl_reembed.py / test_embedding_v6_5.py / migration.sql

## Open Questions
(无则 None)
```

## 约束

1. **只做 #3 范围**(不碰 consolidation / LOCOMO / HNSW / graph / summary)
2. **spec 写功能不写行数**(Pitfall O)
3. **不改 Pydantic 之外库**
4. **不 commit**(主 hermes 自动)
5. **LLM 不需要**(纯后端 embedding 改动, 不调 OpenAI/MiniMax-M3)
6. **如果遇到 #3 不该改的代码**, 写 decision-log.md 说"跳过",不要扩散

## 完成后

1. 写 `.sprint/v6.5.0-phase-1-direction-3-report.md`
2. 主 hermes 自动 commit + finish_task
3. exit 0

如果失败: `.sprint/<task-id>-report.md` 写 `## FAILED` 段说明原因,主 hermes escalate。

---

**开干。auto-pilot mode: 主 hermes 会 6 步验收 (pytest + diff) + commit + finish_task,你只负责写代码**。
