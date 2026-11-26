# Task Spec — v6.5.0 Phase 1 direction #A.3 scheduler

**Scenario**: new-feature
**Complexity**: S
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-A3`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-A3`
**mcp-graph node**: `node_da33db15e3ec`
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 5.2 节 Step 3

---

## 功能点

必做:

1. **新建 `server/src/consolidate/scheduler.py`** (~150 行)
   - `ConsolidationScheduler` 类,后台 cron-style job
   - 触发条件: 新增 chunks ≥ 100 (从 `consolidation_state.last_processed_chunk_id` 算)
   - 每次触发:
     a. 调用 `extract()` (从 #A.1) 处理新 chunks
     b. 调用 `resolve_conflicts()` (从 #A.2) 处理 extract 输出
     c. 更新 `consolidation_state.last_run_at` + last_processed_chunk_id
   - 实现方式: polling (每 N 秒检查) + threading (后台跑)
   - 可配置: `CONSOLIDATION_BATCH_SIZE=100`, `CONSOLIDATION_INTERVAL_SEC=60`

2. **新建 `server/src/consolidate/state.py`** (~30 行)
   - `ConsolidationState` dataclass
   - `last_processed_chunk_id`, `last_run_at`, `total_chunks_processed`

3. **改 `server/src/consolidate/__init__.py`** — export ConsolidationScheduler + ConsolidationState

4. **新建 `server/src/consolidate/CHANGELOG.md`** (append #A.3 段)

## 验收

- pytest `server/tests/test_scheduler_v6_5.py` (新建, 4 个 test):
  1. `test_scheduler_triggers_at_threshold` — 100 chunks 触发
  2. `test_scheduler_skips_below_threshold` — 50 chunks 不触发
  3. `test_scheduler_persists_state` — 跑后状态持久化
  4. `test_scheduler_thread_safe` — 并发触发安全
- 现有 93 tests 无回归

## 不在 #A.3 范围

- 实际 LLM 调用 (跟 #A.1 一样, mock)
- APScheduler / Celery 集成 (留 #A.4)
- API endpoint 暴露 (留 #A.4)

## 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-A3-report.md`
2. 主 arcadia 自动 commit + finish_task
