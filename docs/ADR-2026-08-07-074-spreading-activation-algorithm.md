# ADR-074: Spreading Activation 算法 (V7 PoC-1) (2026-08-07)

- Status: ❌ Rejected (PoC 5 次评估全 FAIL, V7 spread activation 不实施)
- Date: 2026-08-07
- Author: Hermes Agent (MiniMax-M3)
- Depends on: ADR-073 (Rejected)
- Supersedes: 无

## Algorithm

### Core (Collins & Loftus 1975 + 现代变体)

```
输入:
  seeds: List[chunk_id]   # 起始节点 (来自 V6 /search top-K)
  decay: float = 0.7      # 每跳激活强度衰减
  threshold: float = 0.05 # 停止阈值 (激活 < threshold 不再扩展)
  max_hops: int = 3       # 最大跳数
  graph: 邻接表 {chunk_id: {neighbor_id: edge_weight}}

输出:
  List[(chunk_id, activation_score)]  # 按 score 降序

过程:
  activation = {seed: 1.0 for seed in seeds}
  for hop in range(max_hops):
    new_activations = {}
    for node, score in activation.items():
      if score < threshold:
        continue
      for neighbor, edge_weight in graph.get(node, {}).items():
        delta = score * decay * edge_weight
        new_activations[neighbor] = max(new_activations.get(neighbor, 0), delta)
    activation = new_activations
    if not activation:
      break

  return sorted(activation.items(), key=lambda x: -x[1])
```

### 关键参数 (本 ADR 拍板)

| 参数 | 值 | 理由 |
|------|---|------|
| decay | 0.7 | Collins & Loftus 原文 0.7 是常用值 |
| threshold | 0.05 | 经验值, 防止爆炸式扩散 |
| max_hops | 3 | V6 _expand_graph_neighbors_filtered 用 cap=6, 激活扩散用 3 跳足够 (因为会过滤低分) |
| 初始激活 | 1.0 | 种子节点满激活 |
| 边权重 | `memory_relations.strength` | V5 schema 已有 |

### 与 V6 BFS 区别

| 维度 | V6 BFS (`_expand_graph_neighbors_filtered`) | V7 激活扩散 |
|------|--------------------------------------------|-------------|
| 拓扑 | 深度/广度优先, 每节点访问一次 | 概率激活, 节点可被多次更新 (max 合并) |
| 衰减 | 无 | 每跳 × 0.7 |
| 停止 | `min_strength=0.5, cap=6` (硬阈值) | `threshold=0.05` (软阈值, 概率) |
| 输出 | 6 个邻居, 等权 | 任意多节点, 按激活度排序 |
| 召回质量 | 中 (浅层) | 高 (可挖深层) |
| 计算成本 | O(min(N, cap)) | O(N × hops × avg_degree), 一般 < 200 节点 |

## PoC-1 通过标准

### 数据集

合成 **100 chunks**, 形成已知 ground truth 关联网络:

- 10 个 topic cluster (每 cluster 10 chunks)
- cluster 内全连接 (P(edge) = 0.8)
- cluster 间稀疏连接 (P(edge) = 0.1)
- edge_weight 随机 0.3-1.0
- 每个 chunk 有 type / importance / tags

### Ground Truth 标记

每个 cluster 预定义 5 个 "目标 query":
- query 命中 cluster 的 1-2 个 seed
- ground truth = cluster 内其他 8 个相关 chunk (排除 query 自身命中)

### 评估指标

| 指标 | 计算 |
|------|------|
| recall@5 | top-5 结果 ∩ ground truth 大小 / ground truth 大小 |
| recall@10 | top-10 同上 |
| precision@10 | top-10 ∩ ground truth 大小 / 10 |
| 延迟 | 1 query 平均 ms (V6 BFS vs V7 SA) |

### 通过条件

**主条件**: recall@10 (V7 SA) ≥ recall@10 (V6 BFS) + **15%**

**副条件** (任一失败可讨论):
- 延迟 ≤ V6 BFS × 3 (100ms 内可接受)
- precision@10 不退化 (< 5% 下降)
- 100 个 query 都跑成功 (无异常)

### 失败处理

PoC-1 不通过 → ADR-073 → Rejected, V7 整体搁置。

## Implementation Files (PoC-1)

```
server/src/spreading_activation.py    # 独立模块 (不依赖 psycopg2, 可测)
server/tests/test_spreading_activation.py  # 单元测试
scripts/synthesize_memory_graph.py    # 合成数据生成
scripts/verify_spreading_activation.py  # 验证脚本
docs/ADR-2026-08-07-073-v7-spreading-activation-poc.md  # 已写
docs/ADR-2026-08-07-074-spreading-activation-algorithm.md  # 本文件
```

## Unit Tests

| Test | 验证 |
|------|------|
| test_decay_attenuation | 1 跳激活 = seed × 0.7 |
| test_threshold_stops | 激活 < threshold 不再扩展 |
| test_max_hops_limit | 不超过 max_hops |
| test_cycle_safe | A→B→A 环不无限循环 |
| test_isolated_nodes | 无邻居节点不参与扩散 |
| test_orphan_seeds | 种子无邻居 → 只返种子 |
| test_top_k_ranking | 按激活度降序 |

## Verification Plan

### 1. 单元测试 (无需 DB)

```bash
python -m unittest server.tests.test_spreading_activation -v
```

### 2. PoC-1 (合成数据)

```bash
python scripts/synthesize_memory_graph.py --chunks 100 --output /tmp/poc_data.json
python scripts/verify_spreading_activation.py --data /tmp/poc_data.json
# 期望: recall@10 SA ≥ BFS + 15%
```

### 3. (可选, PoC-1 通过后) 真实 2252 chunks sanity

复制 V5/V6 chunks 到 v7_dev schema, 跑同验证脚本, 看相对差异。

## References

- Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407-428.
- Crestani, F. (1997). Application of spreading activation techniques in information retrieval. *Artificial Intelligence Review*, 11(6), 453-482.
- 实施参考: ACT-R cognitive architecture (Anderson 2007)


---

## Rejected 备注 (2026-08-07)

详见 ADR-073 §Rejected 理由. 5 次 PoC 评估 (合成 + 真实数据 + grid search + 权重搜索) 全部 FAIL.

本 ADR 的 `spread_activation.py` 算法实现 + 22 个单元测试 **保留在 `experiments/v7-spreading-activation/` 作为代码档案**, 不进 V6 production.

V6 的 `/search` 主路径仍用 `api_pg.py:_expand_graph_neighbors_filtered` (BFS k-hop + min_strength 过滤).
