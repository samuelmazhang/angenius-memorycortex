# Dispatch — v6.5.0 Phase 1 direction #A.3 scheduler

**收件方**: claude-code (auto-pilot 5/7)

---

## 任务

实现 #A.3 scheduler.py (后台 cron job, 每 100 chunks 触发反思).

## 必做

1. `server/src/consolidate/scheduler.py` (新建, ~150 行) — ConsolidationScheduler 类
2. `server/src/consolidate/state.py` (新建, ~30 行) — ConsolidationState dataclass
3. `server/src/consolidate/__init__.py` — export 新 symbols
4. `server/src/consolidate/CHANGELOG.md` — append #A.3 段

## 必测 (新建 server/tests/test_scheduler_v6_5.py, 4 个 test)

1. `test_scheduler_triggers_at_threshold`
2. `test_scheduler_skips_below_threshold`
3. `test_scheduler_persists_state`
4. `test_scheduler_thread_safe`

## 完成品

写 `<worktree>/.sprint/v6.5.0-phase-1-direction-A3-report.md`

## 约束

1. 只做 #A.3 范围, 不要改 #A.1 extract.py / #A.2 conflict_resolve.py
2. **不要在 worktree 根创建 tests/ 顶层目录** (P31), 用 `server/tests/test_scheduler_v6_5.py` 嵌套
3. spec 写功能不写行数
4. 不调 LLM (mock extract/resolve_conflicts)
5. 不 commit (主 arcadia 自动)
6. **如果 stall**, 写 `<worktree>/.sprint/STALL_REPORT.md`, exit 0 (P30 重派)

## stall 检测 (P30 综合判断)

主 arcadia 不每分钟 poll (P32). 等 `notify_on_complete` 自动 push.
判断标准: writing/reading 动作 + uptime + worktree 文件, 三方一致.

完成后 exit 0.
