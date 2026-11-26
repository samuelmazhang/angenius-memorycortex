# ADR-262: Real-time similar-relation graph (v6.5.1 P2 auto-relate)

- Status: Accepted
- Date: 2026-09-01
- Author: Hermes (subagent dispatch) + 天行健 (decision)
- Depends on: v6.5.0 P2 scheduler (`server/src/consolidate/scheduler.py`),
              memory_chunks.embedding (pgvector 768-dim, HNSW cosine_ops),
              memory_relations table

## 背景 (Context)

`/stats` reports `total_relations: 911`. Investigation found:

  * memory_relations: 911 rows (similar 557, tag-shared 321, same-source
    32, replaced 1)
  * memory_links: 1388 rows (related 1308, implements 23, etc.)
    **/stats was not counting this table**
  * Total real graph size: 911 + 1388 = 2219 relations across 2543 chunks
  * The 557 'similar' rows were a one-time /merge-duplicates run from
    v6.3.x days (LIMIT 100 cap). Nothing has been adding new 'similar'
    rows since.

P2 worker (`/admin/maintenance/stats` shows is_running=true and
total_chunks_processed=129+) was processing every new chunk through
extract + resolve_conflicts but NOT writing any memory_relations rows.
resolve_conflicts only handles taxonomy_boost + conflict_resolution,
not similarity-based relation creation. The relation graph was
frozen.

## 决策 (Decision)

### C: /stats now reports both relation tables

Pre-fix `/stats` and `/health` only counted memory_relations. Three
endpoints fixed: `/stats`, `/health`, `/storage/health`. New fields:
`total_relations_main`, `total_relations_links`.

### D: v6.5.1 P2 auto-relate hook

Scheduler `_worker_tick` now fires `on_process_complete(cid)` after a
chunk is successfully processed. api_pg.py wires this to
`_sched_record_similar_relations(cid)`, which:

  1. Reads the chunk's embedding (early return if NULL).
  2. SELECTs top-5 nearest neighbors via pgvector cosine distance
     using HNSW index. Cosine distance threshold < 0.50 (i.e.
     similarity >= 0.50).
  3. INSERT INTO memory_relations (memory_id_1=new_chunk,
     memory_id_2=neighbor, relation_type='similar',
     strength=cosine_similarity) ON CONFLICT DO UPDATE SET
     strength = EXCLUDED.strength, updated_at = NOW().

Threshold at 0.50 (similarity) because live testing showed the new
chunks have max cosine similarity ~0.50 to any existing chunk in the
~2500-row data set. 0.70 was too aggressive -- the graph would never
grow. 0.50 means "the two chunks are strongly related in embedding
space" and avoids noisy relations between unrelated content.

## 数量 (Numbers)

Pre-deploy: 911 memory_relations rows (all v6.3.x era).
Post-deploy live test: 911 -> 915 (+4 relations written via P2 hook).
Backfill estimate: 2543 chunks * ~5 relations each = ~12700 relations
in one pass.

## 风险 (Risks)

1. **Performance**: each new chunk triggers one vector query (HNSW,
   O(log n)). At 100 chunks/sec write rate this is ~100 HNSW
   queries/sec, well within PG capacity.
2. **Noise**: threshold 0.50 may be too low once the corpus grows.
   Tune via ADR amendment if /stats shows >50 relations per chunk.
3. **Self-relations**: ON CONFLICT correctly dedups via the UNIQUE
   (memory_id_1, memory_id_2) constraint.
4. **Symmetry**: only (new, neighbor) is written. Bi-directional view
   is achieved at search time (cortex already does this in
   `/relations/{memory_id}` which queries both memory_id_1 and
   memory_id_2). No need to write both directions.

## Verification

End-to-end live test (2026-09-01):
  POST /chunks "v6.5.1 P2 real-time auto-relate final test: cap
  theorem tradeoffs between consistency availability partition
  tolerance" -> P2 worker tick -> auto-relate hook fired ->
  1 neighbor (sim=0.726) matched -> 1 relation inserted ->
  /stats.total_relations went 2299 -> 2300.

Unit tests (server/tests/test_scheduler_v6_5.py::_OnProcessCompleteHook,
6 tests, 41/41 total pass): fires-after-process, not-called-on-fail,
failure-doesnt-propagate, optional-default, invalid-type-rejected,
correct-chunk-id.

## Future work

1. **Backfill the existing 2543 chunks**: one-shot script that calls
   enqueue_chunk_id for every chunk_id so the P2 worker generates
   relations for the historical corpus. Estimate: ~12500 relations.
2. **Bigger threshold**: once the data set has >10k chunks, raise
   threshold back to 0.65.
3. **Relation decay**: existing /reconsolidate handles co-occurrence.
   Could extend it to also decay stale 'similar' relations.
