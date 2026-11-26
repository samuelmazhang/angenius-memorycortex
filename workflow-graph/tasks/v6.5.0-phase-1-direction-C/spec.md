# Task Spec — v6.5.0 Phase 1 direction #C summary compaction

**Scenario**: new-feature
**Complexity**: M (2-3 人天)
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-C`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-C`
**mcp-graph node**: `node_4cd220af7a96`

---

## 功能点

必做:

1. **新建 `server/src/consolidate/compact.py`** (~200 行)
   - `compact_long_conversation(chunks, target_tokens)` 
   - 把超长对话 (chunks 数量 > threshold) 压缩成 summary
   - 保留关键 fact + 删除冗余 chunk
   - 写新 `memory_compressed` 表 (schema 已存在)

2. **新建 `server/src/consolidate/summary.py`** (~100 行)
   - `summarize_chunks(chunks)` — mock LLM-friendly (inject callable)
   - test 用 stub 返回固定摘要

3. **改 `server/src/consolidate/__init__.py`** — export compact + summarize

4. **新建 `server/src/consolidate/CHANGELOG.md`** (append #C 段)

## 验收

- pytest `server/tests/test_compact_v6_5.py` (新建, 4 个 test):
  1. `test_compact_preserves_key_facts`
  2. `test_compact_drops_redundant_chunks`
  3. `test_compact_respects_target_tokens`
  4. `test_compact_idempotent` (重复跑安全)

## 约束

1. 只做 #C 范围, 不改 #A.1/#A.2/#A.3
2. **不要在 worktree 根创建 tests/** (P31)
3. spec 写功能不写行数
4. 不 commit (主 arcadia 自动)

## 完成标准

写 `.sprint/v6.5.0-phase-1-direction-C-report.md`
