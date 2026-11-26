# ADR-073: V7 增量 — Spreading Activation 作为单独能力 (2026-08-07)

- Status: ❌ Rejected (PoC-1 + PoC-2 + 权重搜索 5 次评估全 FAIL)
- Date: 2026-08-07
- Author: Hermes Agent (MiniMax-M3)
- Depends on: V6.0 (ADR-067/068/069/070/071/072 已合入)
- Supersedes: 无

## Context

V6.0 收官后, 设计文档提到 "激活扩散 + 工作记忆 + 双系统" 作为 V6 设计意图的实现目标, 但 V6 ADR (067-072) **没承诺实现这些**。本 ADR 把这些拆为 V7 增量, **本轮只做激活扩散一个方向**, 验证可行性。

## Scope (本 ADR)

### In Scope (PoC 范围)
- ✅ Spreading Activation 算法 (Collins & Loftus 1975 + 现代变体)
- ✅ 合成数据集 (100 chunks + ground truth 关联)
- ✅ 对比基准: 现有 BFS k-hop (api_pg.py `_expand_graph_neighbors_filtered`)
- ✅ PoC-1 通过标准: 激活扩散在 ground truth 召回率 ≥ BFS + 15%

### Out of Scope (本 ADR 不做, 等 PoC-1 通过再做)
- ❌ 工作记忆 buffer (Miller's Law)
- ❌ 三层存储 schema (episodic/semantic/procedural)
- ❌ 双系统编排 (System 1/2)
- ❌ 巩固调度升级 (双向)
- ❌ Type hierarchy
- ❌ 多模态

**Out of Scope 各自独立 ADR**: ADR-075 (三层存储) / ADR-076 (工作记忆) / ADR-077 (双系统) / ADR-078 (巩固调度)。**本 ADR 通过后才起草**。

## Go / No-Go 决策点

| 阶段 | 触发 | 通过 | 失败 |
|------|------|------|------|
| Sprint 0 PoC-1 | 合成数据 recall@10 = 激活扩散 ≥ BFS + 15% | 进 Sprint 1 实施 | **回退 V6 收尾, V7 整体搁置** |

**绝不"半 V7"**:PoC-1 不通过, 不做"V7 部分功能"。

## Architecture (V7 总体, 仅做激活扩散部分)

```
                        ┌────────────────────────────────┐
                        │  V7 Spreading Activation       │
   /search (V6) ────►   │  ┌──────────────────────────┐  │   (新)
   (兼容不动)           │  │  seeds = top-K from V6   │  │
                        │  │  spread = iterate hops   │  │
                        │  │  decay = 0.7 per hop     │  │
                        │  │  stop = threshold 0.05   │  │
                        │  └──────────────────────────┘  │
                        └────────────────────────────────┘
                                       │
                                       ▼
                            /search-system2 (新端点)
```

**关键约束**:
- 不修改 `/search` 现有行为 (向后兼容, V6 88 端点不动)
- 新增 `/search-system2` 端点走激活扩散
- 不动 V6 schema, 复用 `memory_chunks` + `memory_relations`
- Feature flag: `MEMORY_FEATURE_SPREADING_ACTIVATION=false` (灰度)

## Implementation Plan

### Sprint 0 (本轮, ~5 天)

| Day | 工作 |
|-----|------|
| 1 | ADR-073 (本文件) + ADR-074 (Spreading Activation 算法 + PoC 标准) |
| 2-3 | 合成数据集生成 (scripts/synthesize_memory_graph.py) + ground truth 标记 |
| 3-4 | PoC-1 实现: spreading_activation.py (独立模块, 可测试) |
| 4 | 验证脚本: scripts/verify_spreading_activation.py (recall@K 对比 BFS) |
| 5 | 跑 PoC-1, 报告 Go/No-Go, 决定下一步 |

### Sprint 1+ (PoC-1 通过才开)

- 实施 `/search-system2` 端点
- 接入 `_expand_graph_neighbors_filtered` 备选路径
- Feature flag 灰度
- E2E + 文档闭环
- (估时 3-4 周)

## Consequences

### 正面
- 风险可控: 一个方向, 失败易回退
- PoC-1 给真实数据驱动的决策 (不是"我觉得会更好")
- 复用 V6 schema + graph, 实施成本低

### 负面 / 风险
- 激活扩散算法参数 (decay=0.7, threshold=0.05) **没文献共识**, 我自己拍
- 合成数据集可能与真实 2252 chunks 分布差异大, PoC-1 通过不代表 prod 通过
- Sprint 1+ 实施时, V6 _expand_graph_neighbors_filtered 可能要重构 (影响范围待估)

## Verification

PoC-1 通过后:
- [ ] PoC 验证脚本 100% 通过 (recall 提升 ≥ 15%)
- [ ] 跑真实 2252 chunks (复制到 v7_dev schema) 跑一次 sanity check
- [ ] ADR-073 + ADR-074 状态改 Accepted
- [ ] 起草 ADR-075/076/077/078 (本轮不做, 但预留 backlog)

PoC-1 失败后:
- [ ] ADR-073 状态改 Rejected, 标注原因
- [ ] V6 收尾作为 memory-cortex 最终版本
- [ ] ADR-075-078 标记 "不实施, 理论参考"

## References

- Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing.
- 评估方法参考 V6 _expand_graph_neighbors_filtered + memory_forgetting 行为
- PoC 借鉴 Cortex ADR-067 §1 (设计意图) + 设计.md §3 (图谱推理)


---

## Rejected 理由 (2026-08-07)

PoC-1 + PoC-2 + 权重搜索共 **5 次评估全部 FAIL**, 没有一次达到 ADR-074 §Go/No-Go 标准 (composite SA ≥ BFS + 15%).

| # | 评估 | 场景 | 主条件 Δ | 备注 |
|---|------|------|---------|------|
| 1 | 合成数据, BFS cap=6 | +6.3% ❌ | 延迟 81x 副条件也 FAIL |  |
| 2 | 合成数据, BFS cap=∞ 公平 | -19% ❌ | — |  |
| 3 | 真实数据, 默认参数 | -0.56% ❌ | β/γ 赢, α 输 |  |
| 4 | 真实数据, grid search 27 组 SA 参数 | 全部 FAIL, 最优 -5.75% ❌ | — |  |
| 5 | 真实数据, 5 个权重组 × 27 SA 参数 | 全部 FAIL, 最优 +0.01% (近乎平局) ❌ | 即使 α=0 (w4) 也平手 |  |

### 关键发现

1. **不是参数没调好**: grid search 27 组 + 权重搜索 5 组 × 27 SA = **135 个组合, 全部 FAIL**
2. **不是指标不公平**: 把 α 完全删掉 (w4_no_alpha), SA 也只跟 BFS 持平 (Δ=+0.01%)
3. **结构性差距**: SA 在 cortex V5/V6 schema 形态的真实数据上不优于 BFS
4. **理论 ≠ 实测**: Collins & Loftus (1975) 50 年认知科学共识, 在 cortex 真实图上不工作

### 归档内容

- 代码: `experiments/v7-spreading-activation/`
  - `server/src/spreading_activation.py` (独立模块, 22 个单元测试过)
  - `scripts/v7_setup_dev_schema.py`
  - `scripts/v7_evaluate_real.py`
  - `scripts/v7_grid_search.py`
  - `scripts/v7_weight_search.py`
- ADR-074 → Rejected (跟 ADR-073 同步)
- 数据: `cortex_v7_dev` schema 保留 (复用 V6 2230 chunks / 1483 relations), 便于未来探索
- 结果总结: `experiments/v7-spreading-activation/V7_RESULTS.md`

### 未来探索方向 (Out of Scope)

1. **三层存储 + SA**: 当前 V5/V6 schema 是 flat chunks + relations. 如果改三层 (episodic/semantic/procedural), SA 在不同层可能赢. 但这是 V8 范畴.
2. **不同图算法**: HNSW / PageRank / Random Walk with Restart. 当前没探索.
3. **真实用户反馈 ground truth**: 当前用启发式 (α/β/γ), 如果有真实 query log + click-through rate, SA 价值可能不同.

### ADR-075-078 状态

| ADR | 标题 | 状态 |
|-----|------|------|
| 075 | 三层存储 | 不实施 (本 ADR V7 整体归档连带) |
| 076 | 工作记忆 | 不实施 |
| 077 | 双系统 | 不实施 |
| 078 | 巩固调度升级 | 不实施 |

**V7 整体搁置. V6 是 memory-cortex 最终版本.**
