# Task Spec — v6.5.0 Phase 2 #NEW3 LLM-extracted fact 质量监控

**Scenario**: new-feature
**Complexity**: XS (0.5 人天, 最小)
**Worktree**: `..worktrees/v6.5.0-phase-2-direction-NEW3`
**Branch**: `v6.5.0/v6.5.0-phase-2-direction-NEW3`
**mcp-graph node**: `node_b5b0ba3cca64`

---

## 功能点

必做:

1. **新建 `server/src/consolidate/quality.py`** (~80 行)
   - `FactQualityMonitor` 类
   - `analyze_confidence_distribution(facts: List[dict]) -> Dict`
     返回 `{"low_quality_count", "avg_confidence", "low_quality_ratio", "needs_review_ids"}`
   - `low_quality_threshold: float = 0.5` (configurable)
   - 标记 fact_confidence < threshold 为 low-quality
   - 返回 needs_review_ids 列表 (低置信 fact 的 ids)

2. **新建 `server/src/consolidate/CHANGELOG.md`** (append NEW3 段)

3. **改 `server/src/consolidate/__init__.py`** — export FactQualityMonitor

## 验收

- pytest `server/tests/test_quality_v6_5.py` (新建, 3 个 test):
  1. `test_quality_monitor_marks_low_confidence`
  2. `test_quality_monitor_threshold_configurable`
  3. `test_quality_monitor_returns_review_ids`

## 约束

1. 只做 NEW3 范围, 不改 #A.1 / #A.2 / #A.3
2. **不要在 worktree 根创建 tests/** (P31)
3. spec 写功能不写行数
4. 不调 LLM (纯分析函数)
5. 不 commit (主 hermes 自动)

## 完成标准

写 `.sprint/v6.5.0-phase-2-direction-NEW3-report.md`
