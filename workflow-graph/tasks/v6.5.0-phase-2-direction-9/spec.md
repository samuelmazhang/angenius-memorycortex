# Task Spec — v6.5.0 Phase 2 #9 RAPTOR 递归摘要层

**Scenario**: new-feature
**Complexity**: XL (7 人天)
**Worktree**: `..worktrees/v6.5.0-phase-2-direction-9`
**Branch**: `v6.5.0/v6.5.0-phase-2-direction-9`
**mcp-graph node**: `node_00eddafac007`

---

## 功能点

必做:

1. **新建 `server/src/consolidate/raptor.py`** (~400 行)
   - `RaptorTree` 类
   - `build_tree(chunks, max_depth=3, cluster_threshold=0.7)`
   - 多层 cluster (UMAP + GMM 或简单 k-means + cosine sim)
   - 每层 cluster 摘要 (LLM-friendly via injected summarize_fn)
   - 返回 `RaptorTree` (root + children + leaf_to_path)

2. **新建 `server/src/consolidate/raptor_query.py`** (~150 行)
   - `query_with_raptor(question, raptor_tree, top_k=10)`
   - traverse tree from root → match cluster → collect leaves
   - 返回 context chunks (按 relevance 排序)

3. **新建 `server/src/consolidate/__init__.py`** — export RaptorTree + query_with_raptor

4. **新建 `server/src/consolidate/CHANGELOG.md`** (append #9 段)

## 验收

- pytest `server/tests/test_raptor_v6_5.py` (新建, 5 个 test):
  1. `test_build_tree_clusters_similar_chunks`
  2. `test_tree_respects_max_depth`
  3. `test_query_traverses_tree_top_down`
  4. `test_query_collects_relevant_leaves`
  5. `test_raptor_idempotent` (重复跑同 input → 同 tree)

## 依赖 (已有)

- numpy / scikit-learn (cortex 已有)
- 不用 LLM (用 injected summarize_fn)

## 约束

1. 只做 #9 范围, 不改 #NEW3 / #6
2. **不要在 worktree 根创建 tests/** (P31)
3. spec 写功能不写行数
4. 不调 LLM (mock summarize_fn)
5. 不 commit (主 hermes 自动)

## 完成标准

写 `.sprint/v6.5.0-phase-2-direction-9-report.md`
