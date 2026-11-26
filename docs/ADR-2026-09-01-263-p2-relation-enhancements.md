# ADR-263: v6.5.1 P2 relation graph enhancements (threshold 0.65, tag-shared, batch endpoint)

- Status: Accepted
- Date: 2026-09-01
- Author: Arcadia (subagent dispatch) + 天行健 (decision)
- Depends on: ADR-262 (real-time similar relation graph)

## 背景 (Context)

ADR-262 实现了 P2 worker 在每条 chunk 入库时自动写 similar relations。Backfill
跑完后 memory_relations.similar 涨到 12,764 (from 558), 平均 5 relations/chunk。
但用户反馈三个问题:

  (a) **similar threshold 0.50 太松**: 大量 weak 关系 (0.50-0.65 band), 噪音多
  (b) **没有 tag-shared relation**: 现有 memory_relations.tag-shared (321 rows) 都是
      v6.3.x 时代的 batch 创建, P2 worker hook 只写 similar
  (c) **/relations/{id} 只支持 single seed**: 多 anchor recall 需要 N+1 round-trips,
      /graph/khop 只支持 single seed

## 决策 (Decision)

### (a) Tighten similar threshold 0.50 -> 0.65

单一常量修改 `_sched_record_similar_relations`:
  * cosine distance WHERE threshold: 0.50 -> 0.35 (= similarity >= 0.65)
  * post-check sim < 0.50 -> < 0.65
  * docstring updated

**Trade-off**: graph 更稀疏 (~30-40% reduction in 'similar' count), 但 precision ↑
每条新 chunk 平均 0-3 relations (vs 4.85 at 0.50).

### (b) Add tag-shared auto-relate hook

Helper `_sched_record_tag_shared_relations(chunk_id)`:
  1. Read chunk's tags (JSONB array).
  2. SQL pre-filter: `tags ?| chunk_tags::text[]` (JSONB 'contains any' operator,
     uses GIN index when available).
  3. Python counts actual overlap. Threshold: >=2 shared tags (cortex chunks
     typically 1-5 tags, so 2-shared eliminates noise from generic tags
     like 'quantplatform' or 'cortex').
  4. INSERT INTO memory_relations (memory_id_1, memory_id_2,
     relation_type='tag-shared', strength=overlap_count)
     ON CONFLICT DO UPDATE SET strength = EXCLUDED.strength.

strength stores overlap count directly (e.g., 3.0 if 3 tags shared) for
operator transparency.

Hook runs unconditionally (even if similar returned 0 rows) -- a chunk
with 3 matching tags should still link to other tag-matching chunks
even if no embedding neighbor passed 0.65.

### (c) Add GET /relations/batch

New endpoint for multi-anchor graph walks. Single round-trip replaces
N+1 calls to /relations/{id}.

Query params:
  * ids: comma-separated chunk_ids, max 32
  * depth: 1..3, default 1 (direct neighbors only)
  * max_per_node: 1..100, default 10

Implementation: PostgreSQL recursive CTE (depth>=2) for server-side
traversal. Undirected graph (queries both memory_id_1 and memory_id_2
directions). Edge filter strength >= 0.5.

Response shape:
```json
{
  "ids": ["id1", "id2"],
  "depth": 2,
  "max_per_node": 10,
  "seeds": {
    "id1": {"neighbors": [...], "hop1_count": 5, "hop2_count": 12},
    "id2": {"neighbors": [...], "hop1_count": 3, "hop2_count": 7}
  },
  "graph": {
    "edges": [{"from_id", "to_id", "relation_type", "strength", "hop"}, ...],
    "nodes": ["id1", "neighbor_id1", "hop2_id1", ...]
  }
}
```

Use cases:
  * 'give me neighbors of these 3 anchors in one call' (use seeds[id].neighbors)
  * 'subgraph induced by {id1, id2, id3}' (use graph.edges)
  * 'count how many multi-hop paths connect id1 and id2' (use graph.edges)

Bug found during testing: FastAPI route order matters. /relations/batch
must be registered BEFORE /relations/{memory_id} or FastAPI matches
'memory_id=batch'. Fix: decorator order in file.

## Verification

### (a) live test
  POST chunk with generic content (low embedding similarity to corpus):
  * Before: similar count = 12764
  * After: similar count = 12764 (0 new relations because sim<0.65)
  * Confirms threshold is enforced

### (b) live test
  POST chunk with tags `["verify-651", "real-time-relation-test-v2"]`:
  * Found 1 neighbor (`mem_20260901_145108_245798_159a72`) via tag overlap
  * strength=2.0 (2 shared tags)
  * Confirms tag-shared hook runs even when similar returns 0 rows

### (c) live test
  GET /relations/batch?ids=SEED&depth=1:
    seeds.SEED.hop1_count = 2, graph.edges = 2, graph.nodes = 3
  GET /relations/batch?ids=SEED,SEED2&depth=2&max_per_node=3:
    graph.edges = 18, graph.nodes = 11, per-seed hop1_count valid,
    per-seed hop2_count valid
  Validation errors:
    depth=0 -> 400 'depth must be 1..3'
    empty ids -> 400 'ids must contain at least one chunk_id'

Unit tests: 41/41 still pass (no behavioral changes to similar-hook tests).

## Future work

1. **Decay stale tag-shared relations**: similar hook only creates; no decay
   mechanism. Could extend /reconsolidate to also clean tag-shared rows
   where one chunk lost the tag.
2. **PPR graph walk** (ADR-260 future): /relations/batch + Personalized
   PageRank for 'find semantically relevant chunks across the whole graph'.
3. **Stream large batch responses**: current cap is 32 seeds. For larger
   tasks, switch to streaming chunks via Server-Sent Events.
