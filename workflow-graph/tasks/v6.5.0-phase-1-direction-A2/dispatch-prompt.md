# Dispatch — v6.5.0 Phase 1 direction #A.2 conflict_resolve

**收件方**: claude-code (auto-pilot 4/7)

---

## 任务

实现 #A.2 ADR-068 conflict_resolve SUPERSEDE/DEDUPE (接 extract() 输出, M spec).

## 必做 (3 步)

1. **server/src/consolidate/conflict_resolve.py** (新建, ~250 行)
   - `resolve_conflicts(new_chunks, db_query, db_execute) -> List[ConflictAction]`
   - SUPERSEDE/DEDUPE/KEEP 三种 action
   - 写 memory_conflict_log

2. **server/src/consolidate/__init__.py** — export `resolve_conflicts`

3. **server/src/consolidate/CHANGELOG.md** — 实现说明

## 必测 (新建 server/tests/test_conflict_resolve_v6_5.py, 5 个 test)

1. `test_supersede_detects_conflicting_value`
2. `test_dedupe_detects_duplicate_fact`
3. `test_keep_writes_new_chunk`
4. `test_conflict_log_records_action`
5. `test_resolve_conflicts_is_idempotent`

## 完成品

写 `<worktree>/.sprint/v6.5.0-phase-1-direction-A2-report.md`

## 约束

1. 只做 #A.2 范围
2. **不要在 worktree 根创建 tests/ 顶层目录, 用 server/tests/conflict_resolve_v6_5.py 嵌套路径** (P31)
3. **不要接管 #A.1 extract.py 代码, 只写 conflict_resolve.py**
4. spec 写功能不写行数
5. 不调 LLM (纯逻辑)
6. **不 commit** (主 arcadia 自动)
7. **如果 stall**, 写 `<worktree>/.sprint/STALL_REPORT.md` 说明中断点, exit 0

## stall 检测 (P30 简化)

主 arcadia 每 1 分钟看进度:
- 跑 >10 分钟无新输出 + worktree 无产物 → kill + 重派同一 task
- 跑 >10 分钟 + .sprint/report.md 已写 → kill + 主 arcadia 6 步验收
- 跑 >15 分钟完全没动 → kill + 重派

完成后 exit 0。
