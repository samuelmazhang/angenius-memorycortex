# Task Spec — v6.5.0 Phase 1 direction #B graph index 社区检测 + HippoRAG PPR

**Scenario**: new-feature
**Complexity**: L (3-5 人天)
**Worktree**: `..worktrees/v6.5.0-phase-1-direction-B`
**Branch**: `v6.5.0/v6.5.0-phase-1-direction-B`
**mcp-graph node**: `node_068b77fea072`

---

## 功能点

必做:

1. **新建 `server/src/graph/community.py`** (~150 行)
   - `detect_communities(edges, min_size)` 用 Leiden 或 Louvain 算法
   - 返回 `Dict[int, Set[str]]` (community_id -> chunk_ids)

2. **新建 `server/src/graph/ppr.py`** (~200 行)
   - `personalized_pagerank(graph, source_nodes, alpha, top_k)`
   - 返回 top-k 节点 (按 PPR 分数排序)

3. **新建 `server/src/graph/__init__.py`** — export

4. **新建 `server/src/graph/CHANGELOG.md`** (新建)

## 验收

- pytest `server/tests/test_graph_v6_5.py` (新建, 4 个 test):
  1. `test_community_detection_simple` — 5 节点 3 社区
  2. `test_ppr_returns_top_k` — mock graph, 跑 PPR, 断言 top-k 节点
  3. `test_ppr_handles_disconnected` — 不连通图处理
  4. `test_community_and_ppr_combined` — end-to-end HippoRAG 风格

## 约束

1. 只做 #B 范围
2. 不动 consolidation / embedding / HNSW
3. **不要在 worktree 根创建 tests/** (P31)
4. spec 写功能不写行数
5. **不调 LLM** (纯图算法)
6. 不 commit (主 hermes 自动)

## 完成标准

写 `.sprint/v6.5.0-phase-1-direction-B-report.md`
