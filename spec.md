# Task Spec — v6.5.0 Phase 1 direction #B graph index + HippoRAG PPR

**Scenario**: new-feature
**Complexity**: M
**Tokens budget**: 5000
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-B`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-B`
**mcp-graph node**: `node_068b77fea072`
**Plan ref**: docs/改进方案-cortex-v6.5.0-完整版-20260822.md 第 9 节 (PRD §9 / roadmap §9)

---

## 功能点 (主 arcadia 写,子代理实现)

### 目标

把 `include_related=true` 的"min_strength 阈值"硬切升级为 HippoRAG 风格的
**个性化 PageRank (PPR)**, 召回更多弱关联 chunk, 多跳 QA recall +20%。

### 必做

1. **Graph index schema 升级** (`server/docker/unified/schema.sql` + 新建 `server/src/migrations/2026_08_23_graph_indexes.sql`)
   - `memory_relations` 加 GIN 复合索引 (覆盖 `(memory_id_1, memory_id_2, strength)` 反向扫描)
   - `memory_links` 加 GIN 复合索引 (覆盖 `(source_id, target_id, strength)` 反向扫描)
   - `memory_chunks` 加 `(importance, archived, model_version)` 复合索引, 让 PPR 收敛后 ranking 更快
   - **新建** `memory_graph_communities` 表 (community_id, member_id, algorithm='leiden'|'louvain', partition_at)
     + `memory_graph_ppr` 表 (node_id, score, anchor_id, computed_at) 缓存 PPR 结果, 避免每次 search 都重算

2. **`server/src/graph/` 新包** (仿 `server/src/consolidate/` 风格)
   - `server/src/graph/__init__.py` — 公开 API 出口
   - `server/src/graph/index.py` — `GraphIndex` 类: 从 `memory_relations` + `memory_links` 拉边
     构造内存图 (adjacency dict + weights), 支持纯 stdlib (no networkx)
   - `server/src/graph/community.py` — `detect_communities()` + Leiden/Louvain
     **fallback 链**: leidenalg → python-louvain → 自实现 label-propagation
     返回 `List[Community]` (id, member_ids)
   - `server/src/graph/ppr.py` — `personalized_pagerank(adj, seeds, alpha=0.85, max_iter=100, tol=1e-6)`
     纯 stdlib power iteration; 返回 `Dict[node_id, score]` (top-K by score)
   - `server/src/graph/cache.py` — PPR 结果缓存到 `memory_graph_ppr` 表
     (anchor_id 主键, computed_at TTL = 1h, 命中即跳过重算)
   - `server/src/graph/CHANGELOG.md` — 模块 changelog

3. **`server/src/api_pg.py` search 集成**
   - 新函数 `_expand_graph_neighbors_ppr(ids, query_emb, alpha=0.85, top_k=10, lex_hit_ids=...)` —
     用 GraphIndex + PPR 替换 `_expand_graph_neighbors_filtered` 的 strength 阈值逻辑
   - `POST /search` (line ~1520) 调用点切换: `req.include_related=True` 时走 PPR 路径,
     默认保留旧 strength 路径 (feature flag `MEMORY_FEATURE_GRAPH_PPR` 默认 **true**)
   - 加 `GET /graph/communities?algorithm=leiden` (可选 — 调试用)
   - 加 `GET /graph/ppr?anchor_id=X&top_k=10` (可选 — 调试用)

4. **CHANGELOG.md** — append #B 段 (跟 #A.3 风格一致: 触发条件, 接口, 性能, 设计约束)

### 验收 (主 arcadia 自动跑)

- pytest `server/tests/test_graph_v6_5.py` (新建, 4 个 dispatch-mandated test):
  1. `test_graph_index_builds_from_relations` — GraphIndex 从 mocked query
     拉 relations + links 构造 adjacency dict, 节点数 = 去重 chunk id 数
  2. `test_community_detection_finds_clusters` — 已知双团结构 (团 A: {1,2,3}, 团 B: {4,5,6},
     跨团边只有 1 条) → detect_communities 返回 2 个 community, 团 A/B 各成一组
  3. `test_ppr_returns_seed_anchored_scores` — PPR(seed={A}) 在双团图上:
     seed 节点 score 最高, 同团节点次之, 跨团节点最低 (数量级差异可验证)
  4. `test_ppr_search_integration_avoids_weak_links` — mock api_pg query: 弱 strength 邻居
     (sim=0.2) 通过 PPR 进入 top-K (被传统 strength 阈值漏掉的现在被召回)

- pytest `server/tests/` (现有 116 tests) 无回归
- spec.md 行数不写, 写功能维度 (trigger conditions, 接口, 性能, 约束)

### 不在 #B 范围

- 实际 LLM 抽取 (#A.1 范围)
- 调度器 (#A.3 范围)
- LOCOMO 评测 harness (#4 范围)
- 长对话 summary (#C 范围)
- pgvector binary quantization (用户禁用)

### 完成标准

1. 写 `<worktree>/.sprint/v6.5.0-phase-1-direction-B-report.md`
2. 主 arcadia 自动 commit + finish_task