# Changelog — `server/src/graph/`

Cortex v6.5.0 graph utilities (方向 #B). HippoRAG / GraphRAG style
community detection + personalized PageRank for the `include_related=true`
recall path.

## 2026-08-23 — v6.5.0 Phase 1 方向 #B (`index.py`, `community.py`, `ppr.py`, `cache.py`)

**Step 1-4 of 4 in the graph-index track.** Replaces the
`min_strength=0.5` flat threshold in `_expand_graph_neighbors_filtered`
with a Personalized PageRank walk over the relation + link graph,
seeded at the lexical + vector hit chunk ids. The expected effect (per
HippoRAG 2 / arXiv:2502.14802) is +20% recall on multi-hop QA while
preserving strong-link recall.

### Trigger conditions (per M spec §9.1 Step 4)

| Condition | Action |
|---|---|
| `req.include_related=true` AND `MEMORY_FEATURE_GRAPH_PPR=true` | Run PPR over the working set |
| `req.include_related=true` AND feature flag **false** | Fall back to legacy strength threshold (v6.3.1 path) |
| `req.include_related=false` | Skip entirely (no graph work) |

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `GraphIndex` | class | In-memory adjacency over `memory_relations` + `memory_links`. Pure stdlib (no networkx). |
| `GraphEdge` | dataclass | One undirected weighted edge. |
| `merge_indices` | function | Union of multiple GraphIndex instances. |
| `Community` | dataclass | One detected community: `(id, member_ids)`. |
| `detect_communities(graph, algorithm="auto", seed=42)` | function | Leiden / Louvain / label-propagation. Returns `List[Community]`. |
| `available_algorithms()` | function | List of algorithms available in this env. |
| `community_of(graph, node, communities)` | function | Find the community containing a node. |
| `personalized_pagerank(graph, seeds, alpha=0.85, max_iter=100, tol=1e-6)` | function | Power iteration; returns `{node_id: score}`. |
| `top_k_ppr(graph, seeds, k=10, ...)` | function | PPR + sort + truncate. |
| `normalize_scores(scores)` | function | L1 normalization helper. |
| `compute_or_load(graph, anchor_id, db_query, db_execute, ...)` | function | Cache-aware PPR. |
| `load_cached` / `save_cached` | function | Raw cache I/O. |
| `DEFAULT_TTL_SECONDS` | const (3600) | Cache freshness window. |

### PPR parameters (defaults)

| Param | Default | Source |
|---|---|---|
| `alpha` | 0.85 | Standard PageRank damping |
| `max_iter` | 100 | Convergence cap (L1 < `tol` short-circuits) |
| `tol` | 1e-6 | L1 convergence threshold |
| `top_k` | 10 | Recall-budget cap (matches v6.5.0 #2 cap) |
| `cache_ttl` | 3600s | PPR cache freshness |

### Community detection algorithm chain (auto)

1. `leidenalg` (preferred — ModularityVertexPartition; needs `igraph`)
2. `python-louvain` (fallback when leidenalg absent)
3. `label_propagation` (always available — Raghavan et al. 2007, stdlib)

`detect_communities("auto")` walks the chain and returns on the first
success. Unit tests pin `label_propagation` for determinism.

### Design constraints honored

- **No networkx in cold path.** `GraphIndex` is a plain
  `dict[str, dict[str, float]]`. PPR is a 30-line power iteration.
  Tests run without `networkx` / `leidenalg` installed.
- **DB-decoupled.** GraphIndex reads via injected `db_query` callable;
  PPR is pure math on the in-memory adjacency. The same DI style used by
  `#A.2 conflict_resolve` and `#A.3 scheduler`.
- **Schema-additive.** New tables (`memory_graph_communities`,
  `memory_graph_ppr`) are independent — no migration impact on
  `memory_chunks` / `memory_relations` / `memory_links`. Production
  databases need a one-time `CREATE TABLE` (see
  `server/src/migrations/2026_08_23_graph_indexes.sql`).
- **Feature-flag gated.** `MEMORY_FEATURE_GRAPH_PPR` defaults to true
  but can be toggled off to A/B against the legacy strength path.
- **Cache TTL bounded.** `memory_graph_ppr` rows older than 1h are
  considered stale; PPR re-runs transparently.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/graph/__init__.py` | new | Public API re-exports. |
| `server/src/graph/index.py` | new | `GraphIndex` + `GraphEdge`. |
| `server/src/graph/community.py` | new | Leiden / Louvain / label-prop. |
| `server/src/graph/ppr.py` | new | Power-iteration PPR. |
| `server/src/graph/cache.py` | new | PPR cache I/O. |
| `server/src/graph/CHANGELOG.md` | new | This file. |
| `server/docker/unified/schema.sql` | updated | Composite indexes + new tables. |
| `server/src/migrations/2026_08_23_graph_indexes.sql` | new | Migration for new tables. |
| `server/src/api_pg.py` | updated | `_expand_graph_neighbors_ppr` + search hook. |
| `server/tests/test_graph_v6_5.py` | new | 4 dispatch-mandated tests + helpers. |
| `.sprint/v6.5.0-phase-1-direction-B-report.md` | new | Sprint completion report. |

### Files **NOT** touched (per dispatch constraint)

- `server/src/consolidate/*` (owned by #A.1 / #A.2 / #A.3).
- `server/src/api_pg.py` callers other than the search / graph endpoints
  (`_expand_graph_neighbors_filtered` is preserved as the legacy path).
- `server/src/memory_taxonomy.py`.
- `server/src/etl/etl_reembed.py` (owned by #3).

### Test coverage

The 4 required tests map as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_graph_index_builds_from_relations` | GraphIndex.from_db builds adjacency from mocked query rows. |
| 2 | `test_community_detection_finds_clusters` | `detect_communities` returns 2 communities on a twin-clique graph. |
| 3 | `test_ppr_returns_seed_anchored_scores` | PPR scores seed > same-cluster > other-cluster. |
| 4 | `test_ppr_search_integration_avoids_weak_links` | Integration: weak-strength neighbors enter top-K via PPR. |

Plus regression tests for `normalize_scores`, `top_k_ppr`,
`compute_or_load` cache hit/miss, schema migrations, and the
`MEMORY_FEATURE_GRAPH_PPR` feature flag.

### Open follow-ups (not in this drop)

- **#A.4 eval**: compare LOCOMO 1986 QA recall with PPR vs strength
  threshold; quantify the +20% target.
- **Phase 2 cross-encoder rerank** (NEW1): re-rank the top-100 from PPR
  with a cross-encoder for the +7% precision bump.
- **Production deployment**: enable `MEMORY_FEATURE_GRAPH_PPR` in
  `server/src/config.py` default to `true` once eval confirms parity.