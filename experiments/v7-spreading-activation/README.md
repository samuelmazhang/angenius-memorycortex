# V7 Spreading Activation 实验总结 (2026-08-07)

> **结论**: V7 spread activation 在 cortex V5/V6 schema 形态的真实数据上**不优于 BFS**,经 5 次评估 (合成 + 真实 + grid search + 权重搜索) 验证. 代码归档到此目录, ADR-073/074 → Rejected.

## 实验时间线

| 时间 | 工作 | 结论 |
|------|------|------|
| 2026-08-07 | 写 ADR-073 (V7 总体定位) | Draft |
| 2026-08-07 | 写 ADR-074 (Spreading Activation 算法 + PoC 标准) | Draft |
| 2026-08-07 | 写 `spreading_activation.py` (独立模块, 22 个单元测试) | OK |
| 2026-08-07 | PoC-1 合成数据 + `verify_spreading_activation.py` | FAIL |
| 2026-08-07 | 建 `cortex_v7_dev` schema (2230 chunks / 1483 relations) | OK |
| 2026-08-07 | 真实数据评估 (`v7_evaluate_real.py`, n=100) | FAIL |
| 2026-08-07 | Grid search 27 组 SA 参数 (`v7_grid_search.py`) | 全部 FAIL |
| 2026-08-07 | 权重搜索 5 组 × 27 SA (`v7_weight_search.py`) | 全部 FAIL |
| 2026-08-07 | **决定: 归档 V7** (per ADR-074 §Go/No-Go) | — |

## 5 次评估结果

| # | 评估 | SA composite | BFS composite | Δ | PASS |
|---|------|--------------|---------------|---|------|
| 1 | 合成 100 chunks, BFS cap=6 | 0.396 | 0.333 | +6.3% | ❌ |
| 2 | 合成 100 chunks, BFS cap=∞ 公平 | 0.396 | 0.585 | -19.0% | ❌ |
| 3 | 真实 2230 chunks, 默认参数 | 0.741 | 0.747 | -0.56% | ❌ |
| 4 | 真实 2230 chunks, grid search 27 组 (最优 decay=0.5/threshold=0.01/max_hops=2) | 0.761 | 0.818 | -5.75% | ❌ |
| 5 | 真实 2230 chunks, 5 权重组 × 27 SA (最优 α=0/β=0.5/γ=0.5) | 0.9305 | 0.9304 | +0.01% | ❌ |

## 关键发现

### 1. 不是参数没调好

135 个组合 (27 grid × 5 weight) **全部 FAIL**. 即使最保守配置 (decay=0.5, threshold=0.01, max_hops=2) 仍是 SA < BFS.

### 2. 不是指标不公平

α 是 BFS 主导赢的维度. 即使把 α 权重设为 0 (w4_no_alpha), SA 也只跟 BFS 持平 (+0.01%). β+γ 加起来 SA 也只跟 BFS 平手.

### 3. 结构性差距

SA 在 cortex V5/V6 schema 形态的真实数据上**没有本质优势**. 可能因为:
- cortex 图**稀疏 + 长尾**: 1454 边 / 1277 节点 = 平均度数 1.14, 远低于社交网络典型 10-50
- **远跳信号衰减太快**: decay=0.7 → 3 跳后信号剩 0.343, 4 跳剩 0.24, 已接近 threshold=0.05
- **BFS cap=6 反而是合理设计**: 在稀疏图上, 浅层遍历覆盖了大部分相关节点

### 4. 理论 ≠ 实测

Collins & Loftus (1975) 50 年认知科学共识. ACT-R 核心架构. **但** cortex 不是认知模拟——它是一个**长期记忆 + 向量检索 + 关系图**的系统, 数据稀疏且噪声大, 跟认知实验的小规模高密度网络不同.

## 文件清单

```
experiments/v7-spreading-activation/
├── README.md (本文件)
├── spreading_activation.py          # 算法实现 (Collins & Loftus 1975 + 现代变体)
├── test_spreading_activation.py     # 22 个单元测试 (全过)
├── v7_setup_dev_schema.py           # cortex_v7_dev schema bootstrap (幂等)
├── v7_evaluate_real.py              # 真实数据 SA vs BFS 评估
├── v7_grid_search.py                # 27 组 decay/threshold/max_hops grid
└── v7_weight_search.py              # 5 组权重 × 27 SA 权重敏感度
```

## 数据保留

- **Schema `cortex_v7_dev` 保留** (127.0.0.1:5432): 2230 chunks + 1483 relations
- 用途: 未来探索 (V8 范畴) 直接复用, 不需要重新 bootstrap

## 未来方向 (Out of Scope, 留待未来)

1. **三层存储 + SA**: V8 范畴, 改 episodic/semantic/procedural 后 SA 可能赢
2. **HNSW / PageRank / Random Walk with Restart**: 不同图算法对比
3. **真实 query log + click-through ground truth**: 当前用启发式, 真实反馈可能改变结论
4. **图密度提高后重测**: 当前 cortex 图太稀疏 (平均度数 1.14), 高密度图上 SA 可能赢

## ADR 索引

- ADR-073: V7 总体定位 + 边界 → ❌ Rejected
- ADR-074: Spreading Activation 算法 → ❌ Rejected
- ADR-075/076/077/078: 各自独立方向 → ❌ 不实施 (随 V7 整体归档)

## 复现实验

```bash
# 1. 跑 grid search
cd .
python experiments/v7-spreading-activation/v7_grid_search.py --n-queries 100

# 2. 跑权重搜索
python experiments/v7-spreading-activation/v7_weight_search.py --n-queries 100

# 3. 单次评估
python experiments/v7-spreading-activation/v7_evaluate_real.py --n-queries 100
```

## 结论

**V7 spread activation 在 cortex 当前形态下不推荐**. V6.0 (ADR-067-072) 是 memory-cortex 最终版本. V7 整体搁置, 代码 + ADR 留档作为未来探索参考.
