# Phase 3 决策框架

> **Date**: 2026-08-23
> **Status**: 等 W6+ LOCOMO baseline 数据 (Phase 3 待定)
> **Decision trigger**: LOCOMO F1/BLEU/EM 数据 + 1 个月 production 数据

---

## Phase 3 候选 (按 ROI 排)

| 方向 | ROI | 工时 | 触发条件 |
|---|---|---|---|
| **NEW1 Cross-encoder rerank** | 7 | 3d | recall < 0.85 (LOCOMO F1) |
| **NEW2 Embedding hot-swap** | 6 | 2d | 模型频繁换 / 需要无停机升级 |
| **#10 PG RLS** | 5 | 6d | 真有多用户 (生产监控数据) |

---

## 决策规则 (data-driven)

### Rule 1: NEW1 rerank
- **触发条件**: LOCOMO F1 < 0.85 (基线 0.85)
- **判断**: 如果 v6.5.0 的 #2 + #A + #B + #9 提升到 0.90+ → 不需要 rerank;否则需要
- **数据**: nightly LOCOMO harness (#4) 输出 F1/BLEU/EM monitor 1 个月

### Rule 2: NEW2 hot-swap
- **触发条件**: 未来 3 个月内需要换 embedding model
- **判断**: 工程价值大于 ROI (避免 data migration + downtime)
- **数据**: dev team roadmap (如果有模型升级计划)

### Rule 3: #10 RLS
- **触发条件**: cortex 用户 > 1 OR 客户需要 multi-tenant 隔离
- **判断**: demo scale 没必要; production multi-tenant 必须
- **数据**: 生产 user_count metric

---

## 收集数据 (W1-W6)

1. **LOCOMO baseline** (Phase 1 #4): nightly CI
   - F1 / BLEU / EM / ROUGE-L
   - per-conv breakdown (10 convs)

2. **Production metrics** (Phase 2 #6): OTLP
   - search latency P50/P95/P99
   - consolidation queue depth
   - fact extraction rate
   - conflict supersede rate
   - hallucination rate (NEW3)

3. **User feedback**: 投诉 / 点赞 / 转化

4. **Memory growth**: chunks 数量 / 模型分布 / retention rate

---

## Decision Day (W6)

review W1-W6 数据:
- F1 < 0.85 → 立即 NEW1 (3 天)
- 模型升级计划有 → NEW2 (2 天)
- 多用户出现 → #10 (6 天)

**W6 决策模板** (写这个 doc):

```markdown
## Phase 3 决策 (YYYY-MM-DD)

### 数据 (W1-W6 LOCOMO baseline)
- F1: X.XX (baseline 0.85)
- BLEU: X.XX
- EM: X.XX

### Production metrics
- search latency P95: XXms
- consolidation queue: XX (健康 < 200)
- fact hallucination rate: X.X%

### 决策
- [ ] NEW1 rerank: GO / NO-GO (原因)
- [ ] NEW2 hot-swap: GO / NO-GO (原因)
- [ ] #10 RLS: GO / NO-GO (原因)

### 实施计划
(如果 GO,列出 sprint 计划)
```

---

## 不做的选项 (低优先级)

| 选项 | 原因 |
|---|---|
| Phase 3 现在推 | 没有 baseline, 不知道该不该做 |
| Phase 3 跳过 | NEW1 ROI 7, 实际能补 recall |
| Phase 3 全做 | 12 周后再说, 先稳定 production |
| NEW1 #B PPR 替换 | PPR 是 recall boosting, rerank 是 precision boosting, 互补 |

---

## 参考

- 方案 v3 §12: Phase 3 (5-7 周后, 视规模)
- Phase 1+2 sprint reports: `workflow-graph/tasks/v6.5.0-phase-1*/`
- LOCOMO harness: `tests/eval/test_locomo_v6_5.py`
- OpenTelemetry: `server/src/observability/`

---

**Review date**: 2026-10-01 (W6 after Phase 1+2 deployment)
