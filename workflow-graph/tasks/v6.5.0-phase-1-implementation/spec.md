# Task Spec: v6.5.0-phase-1-implementation

**Created at**: 2026-08-23T02:00:00Z

**Scenario**: new-feature (逐步实施)

**CLI**: claude-code (按用户 2026-08-23 02:00 OOB 决定)

**Worktree**: `..worktrees/v6.5.0-phase-1-implementation`

**Description**:
v6.5.0 Phase 1 实现。先做 #2 召回公式修正(已完成),现在做 #A.1 LLM 抽取层。后续方向在同一 task-dir 持续。

**Plan 文档**: `docs/改进方案-cortex-v6.5.0-完整版-20260822.md` (526 行,22.3KB)

---

## Phase 1 方向清单(7 个,本 sprint 进度)

| # | 方向 | ROI | 工天 | 状态 |
|---|---|---|---|---|
| **#2** ⭐ | 召回公式修正(imp 5%→ 15%) + 新块冷启动 + Pydantic 校验 | 9 | 3.5 | ✓ done |
| #A 🆕 | consolidation pipeline(Mem0-style + ADR-068) | 9 | 8-10 | **当前: #A.1** |
| #3 | Nomic v1.5 embedding 升级 + 增量 re-embed | 8 | 3.5 | 待开 |
| #4 | LOCOMO 1986-case online eval harness | 7 | 7 | 待开 |
| #5 | HNSW m=24 调优 | 4 | 1 | 待开 |
| #B 🆕 | graph index 社区 + HippoRAG PPR | 8 | 3-5 | 待开 |
| #C � | 长对话 summary compaction | 7 | 2-3 | 待开 |

---

## 当前 #A.1 spec

### 目标

新建 `server/src/consolidate/extract.py` (~200 行),实现 `extract(chunk, context_chunks, cfg) -> dict` 函数,
调用 LLM (config.llm_url / llm_api_key / llm_model) 抽取结构化记忆。

### 输入 / 输出

输入:
- chunk: 单个 chunk 全部字段 (id / content / type / tags / source / importance / metadata)
- context_chunks: ±2 邻居 chunks

输出 JSON:
```python
{
  "facts": [{"entity": "...", "predicate": "...", "value": "...", "confidence": 0.0-1.0}],
  "timeline": [{"date": "YYYY-MM-DD or relative", "event": "..."}],
  "preferences": [{"category": "...", "preference": "..."}]
}
```

### 实施要点

1. httpx.AsyncClient + cfg.llm_url + Bearer cfg.llm_api_key
2. tenacity retry: 3 attempts, exponential 30s/60s/120s
3. temperature=0.0
4. LLM 不可达 → 返回空 dict 不抛
5. JSON schema 校验 facts 含 confidence 字段

### 验收 (5 个 test)

1. test_extract_returns_valid_schema
2. test_extract_handles_llm_unreachable
3. test_extract_retries_on_failure
4. test_extract_respects_timeout
5. test_extract_facts_have_confidence

### 不在 #A.1 范围

- conflict_resolve SUPERSEDE → #A.2
- scheduler 每 100 chunks 触发 → #A.3
- eval integration → #A.4

---

## Spec fields (raw JSON)

```json
{
  "task_name": "v6.5.0-phase-1-implementation",
  "scenario": "new-feature",
  "cli": "claude-code",
  "worktree": "..worktrees/v6.5.0-phase-1-implementation",
  "description": "v6.5.0 Phase 1 实现。#2 done, #A.1 当前",
  "tags": ["v6.5.0", "phase-1", "recall-formula+consolidation", "claude-code", "real-dispatch"],
  "plan_doc": "docs/改进方案-cortex-v6.5.0-完整版-20260822.md",
  "phase_1_directions": ["#2", "#A", "#3", "#4", "#5", "#B", "#C"],
  "current_direction": "#A.1",
  "done": ["#2"],
  "in_progress": ["#A.1"]
}
```
