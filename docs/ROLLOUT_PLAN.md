# Cortex v6.5.0 — Rollout Plan (Phase A/B/C)

> **Date**: 2026-08-23
> **Deploy target**: production cortex cluster
> **Strategy**: 渐进式 A/B 灰度 (canary 1% → 10% → 50% → 100%)

---

## 1. 总览

| 阶段 | 时长 | 流量 | 关键监控 |
|---|---|---|---|
| **Canary** | 1-3 天 | 1% | latency / errors / LOCOMO F1 |
| **Phase A** | 3-5 天 | 10% | latency / recall / consolidation queue |
| **Phase B** | 7-14 天 | 50% | user feedback / model_version migration |
| **Phase C** | 持续 | 100% | full monitoring |

**总周期**: 2-3 周 (canary + A + B), Phase C 持续。

---

## 2. 阶段详细

### 2.1 Canary (1% 流量, 1-3 天)

**目标**: 验证基本功能 + 找 critical bug

**操作**:
1. 在 1% 流量的 server 上部署 v6.5.0
2. feature flag 默认 off (除 #2 recall + #5 HNSW + #B PPR 这些已 validated 的)
3. 监控 24-72h:
   - error rate < 0.1%
   - latency P95 < baseline + 50%
   - no critical bug report

**rollback 条件**:
- error rate > 1%
- P95 latency > 2x baseline
- 任何 critical bug (e.g., data corruption)

### 2.2 Phase A (10% 流量, 3-5 天)

**目标**: 验证 #A consolidation pipeline 在 10% 流量下稳定

**操作**:
1. Phase B 流量从 1% → 10%
2. 开启 `#A.1 extract` + `#A.2 conflict_resolve` feature flag (10% 流量上跑 consolidation)
3. 开启 `#NEW3 fact quality` 监控 (不阻塞,只统计)
4. 监控 3-5 天:
   - LOCOMO F1 不退化 (-2% 容差)
   - P95 latency 不退化 (+20% 容差)
   - consolidation queue depth < 200
   - conflict_log 表错误率 < 5%
   - fact quality hallucination rate < 10%

**rollback 条件**:
- LOCOMO F1 < baseline -5%
- P95 latency > 2x baseline
- consolidation queue 堆积 (>500, scheduler 跟不上)
- user 投诉率 > baseline

### 2.3 Phase B (50% 流量, 7-14 天)

**目标**: 验证 #3 Nomic v1.5 embedding 在 50% 流量稳定 + 开启 #9 RAPTOR

**操作**:
1. Phase B 流量从 10% → 50%
2. **开启 #3 Nomic v1.5 灰度**:
   - `EMBEDDING_MODEL=nomic-v1.5` (新 chunks 用 Nomic)
   - 老 chunks 保留 mpnet (背景 ETL 渐进 re-embed)
   - 监控 model_version 分布 (mpnet → nomic-v1.5)
3. **开启 #9 RAPTOR** (50% 流量, query 时用 RAPTOR)
4. 监控 7-14 天:
   - recall F1 +2% (RAPTOR 应该提升)
   - latency P95 < +30%
   - Nomic 推理稳定 (不 OOM, 不超时)
   - user 反馈 (memory retrieval 更准)

**rollback 条件**:
- Nomic 模型反复 OOM / 推理失败
- RAPTOR tree 太深 (>10 levels)
- 50% 流量 latency 飙升

### 2.4 Phase C (100% 流量, 持续)

**目标**: 全面 rollout, 持续监控

**操作**:
1. Phase C 流量从 50% → 100%
2. 全 feature flag on
3. 开启 #6 OpenTelemetry 100%
4. 关闭 #6 (OTel) 仍可
5. 持续监控:
   - weekly LOCOMO harness CI (Phase 1 #4)
   - nightly LOCOMO full 1986 case
   - weekly fact quality audit (NEW3)
   - monthly memory chunk growth (size + count)

**长期监控**:
- 每年 review Phase 3 是否需要 (NEW1 rerank / NEW2 hot-swap / #10 RLS)

---

## 3. Timeline (建议)

| Week | 阶段 | 主要动作 |
|---|---|---|
| **W1** | Canary | 部署 1%, 监控 24h |
| **W1-W2** | Phase A | 10%, 开启 #A consolidation |
| **W3-W4** | Phase B | 50%, 开启 #3 Nomic + #9 RAPTOR |
| **W5+** | Phase C | 100%, 持续监控 |
| **W6+** | 收集 baseline | LOCOMO F1/BLEU/EM, 决定 Phase 3 |
| **12 周后** | Phase 3 review | NEW1 rerank / NEW2 hot-swap / #10 RLS |

---

## 4. Decision Gates

### Gate 1 (Canary → Phase A)
- [ ] error rate < 0.1%
- [ ] latency P95 < 2x baseline
- [ ] no critical bug
- [ ] LOCOMO F1 >= baseline -2%

**Approve**: SRE + Cortex tech lead 共同 sign-off

### Gate 2 (Phase A → Phase B)
- [ ] consolidation queue depth < 200 (3 天稳定)
- [ ] conflict_log 错误率 < 5%
- [ ] fact quality hallucination rate < 10% (NEW3 监控)
- [ ] LOCOMO F1 >= baseline -2%
- [ ] P95 latency < +20%

**Approve**: Cortex tech lead + Product manager

### Gate 3 (Phase B → Phase C)
- [ ] Nomic v1.5 推理稳定 (无 OOM / 超时)
- [ ] RAPTOR query depth < 10 levels
- [ ] 50% 流量 1 周稳定
- [ ] model_version 分布显示 Nomic 占比 > 30%

**Approve**: Cortex tech lead + VP Engineering

---

## 5. Rollback 决策

| 触发 | Action | Owner |
|---|---|---|
| error rate > 5% (any phase) | **Immediately** rollback to v6.3.1 | SRE on-call |
| LOCOMO F1 < baseline -10% (Phase A/B) | Rollback + investigate | Cortex tech lead |
| P95 latency > 3x baseline | Rollback + optimize | SRE on-call |
| Nomic OOM > 5x/hour | Rollback #3 + reopen | SRE on-call |
| data corruption (任何指标) | **Immediately** rollback + freeze writes | VP Eng |

---

## 6. Communication Plan

| 时间 | 受众 | 内容 |
|---|---|---|
| T-7d | Engineering | DEPLOYMENT.md + ROLLOUT_PLAN.md review |
| T-3d | Product | 灰度时间表 + 用户影响 |
| T-0d (Canary) | All | "v6.5.0 in 1% traffic" |
| T+24h | All | "canary 24h results" |
| T+3d (Phase A) | All | "10% rollout begins" |
| T+1w | Product | "Phase A 1 周报告" |
| T+2w (Phase B) | All | "50% rollout begins" |
| T+3w (Phase C) | All | "v6.5.0 fully deployed" |

---

## 7. Post-Deployment Tasks (Phase 2 done 后)

1. **收集 1 个月 LOCOMO baseline** (F1/BLEU/EM)
2. **A/B test**: 比较 v6.3.1 vs v6.5.0 (memory retrieval quality)
3. **Phase 3 decision document** (基于 baseline 决定 NEW1/NEW2/#10)
4. **培训 SRE on-call** (Phase 2 #6 OTel + Phase 1 #A consolidation)
5. **更新 runbook**: 包含 consolidation failure / conflict_log 异常 / Nomic OOM

---

**Hand-off complete. Phase 3 决策待 W6+ baseline 数据收集。**
