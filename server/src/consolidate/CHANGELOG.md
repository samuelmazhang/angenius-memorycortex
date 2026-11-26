# Changelog — `server/src/consolidate/`

Cortex v6.5.0 consolidation pipeline (方向 #A).

## 2026-08-23 — v6.5.0 Phase 1 方向 #A.2 (`conflict_resolve.py`)

**Step 2 of 4 in the consolidation pipeline.** Pure-logic layer that takes
the structured facts emitted by `#A.1 extract()` and applies ADR-068's
SUPERSEDE / DEDUPE / KEEP rules per `(user_id, entity, predicate)` bucket.

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `resolve_conflicts(new_chunks, db_query, db_execute)` | function | Main entry — returns `List[ConflictAction]`, one per fact. |
| `ConflictAction` | dataclass | One decision: `action`, `user_id`, `entity`, `predicate`, `value`, `confidence`, `new_fact_id`, `old_fact_id`, `reason`, `decided_at`. |
| `ACTION_KEEP` / `ACTION_SUPERSEDE` / `ACTION_DEDUPE` | str constants | Three decision labels. |
| `VALID_ACTIONS` | frozenset | Set of the three action labels (for input validation). |

### Decision matrix (M spec — `cortex-v6.5-roadmap.md` §5.2 Step 2)

| Existing fact in bucket? | Same value? | Action | Side effects |
|---|---|---|---|
| no | n/a | **KEEP** | INSERT new row in `memory_extracted_facts`; log row in `memory_conflict_log`. |
| yes | yes | **DEDUPE** | No DB mutation; log row in `memory_conflict_log` (`action='dedupe'`). |
| yes | no | **SUPERSEDE** | INSERT new row + best-effort `is_deprecated=TRUE` on old row (silently skipped if column missing); log row in `memory_conflict_log` (`action='supersede'`). |

### Design constraints honored

- **No LLM.** The dispatch forbids LLM calls; this module is `json` + stdlib only.
- **Pure logic.** All persistence flows through injected `db_query` / `db_execute` callables, so unit tests run without a live Postgres.
- **Schema-additive.** The `fact` column on `memory_extracted_facts` is plain `TEXT`; we JSON-encode the `{entity, predicate, value, confidence}` triple into it. No new columns, no migration.
- **Idempotent.** Re-running `resolve_conflicts` on the same `new_chunks` converges:
  - `KEEP` → on rerun becomes `DEDUPE` (existing row now matches the new value).
  - `SUPERSEDE` → on rerun becomes `DEDUPE` (the freshly-inserted active row matches).
- **Fault-tolerant.** DB errors are caught, logged at `WARNING`, and a synthetic `ConflictAction` (no `new_fact_id`) is emitted so the caller still gets 1:1 row count.
- **Test boundary.** `#A.1 extract.py` is left untouched. The two modules share the `consolidate/` package but import only from stdlib + each other.

### Bucket key

`source_key` for `memory_conflict_log` is derived from the fact itself:

```
source_key = "<lowercased_entity>::<lowercased_predicate>"
```

This is independent of ADR-068's `metadata->>'source_key'` for `memory_chunks`
(those are user-provided tags). The two source_key spaces coexist; the
log table is segregated by `action` prefix.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/consolidate/conflict_resolve.py` | new | The implementation (see above). |
| `server/src/consolidate/__init__.py` | updated | Re-exports `resolve_conflicts`, `ConflictAction`, action constants. |
| `server/src/consolidate/CHANGELOG.md` | new | This file. |
| `server/tests/test_conflict_resolve_v6_5.py` | new | 5 dispatch-mandated tests + helper / regression tests (see test file header). |
| `.sprint/v6.5.0-phase-1-direction-A2-report.md` | new | Sprint completion report. |

### Files **NOT** touched (per dispatch constraint)

- `server/src/consolidate/extract.py` (owned by #A.1).
- `server/src/api_pg.py` (production wiring lands in #A.3 scheduler).
- `server/src/memory_taxonomy.py`.
- `server/docker/unified/schema.sql` (no schema migration needed; everything fits in existing columns).

### Test coverage

The 5 required tests map to the matrix as follows:

| # | Test | Decision exercised |
|---|---|---|
| 1 | `test_supersede_detects_conflicting_value` | SUPERSEDE |
| 2 | `test_dedupe_detects_duplicate_fact` | DEDUPE |
| 3 | `test_keep_writes_new_chunk` | KEEP |
| 4 | `test_conflict_log_records_action` | log row per action |
| 5 | `test_resolve_conflicts_is_idempotent` | rerun converges |

Plus 8 helper / regression tests (`_values_equal`, `_serialize_fact`,
`_deserialize_fact`, edge cases, malformed input).

### Open follow-ups (not in this drop)

- **#A.3 scheduler**: plug `resolve_conflicts(extract(...))` into the
  rolling-window / 24h cron / manual-trigger pipeline.
- **#A.4 eval**: compare LOCOMO 1986 QA before/after conflict resolution.
- **Schema bump** (future, non-blocking): add `is_deprecated` /
  `deprecated_at` columns to `memory_extracted_facts` so the SUPERSEDE
  side-effect becomes observable to read paths. Current implementation
  already does the UPDATE; the log row is the audit trail until then.
---

## 2026-08-23 — v6.5.0 Phase 1 方向 #A.3 (`scheduler.py` + `state.py`)

**Step 3 of 4 in the consolidation pipeline.** The scheduler is the
"trigger" layer that decides *when* to run `#A.1 extract()` and
`#A.2 resolve_conflicts()`. It exposes both a synchronous escape hatch
(`run_once`) and an optional daemon thread (`start` / `stop`) that
checks every `poll_interval` seconds.

### Trigger conditions (per M spec §5.2 Step 3)

| Condition | Source | Default |
|---|---|---|
| `chunks_since_last_run >= threshold` | rolling chunk counter | **100** |
| 24h cumulative new chunks > 50 | optional time-based | (out of scope here; pluggable via `record_chunk`) |
| Manual call (`scheduler.run_once()` or `maybe_run()`) | API / cron | always honored |

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `ConsolidationScheduler` | class | Main entry — see below. |
| `ConsolidationState` | dataclass | Mutable counters (in `state.py`); JSON-round-trippable. |
| `DEFAULT_THRESHOLD` | int (100) | Chunk-count trigger threshold. |
| `DEFAULT_POLL_INTERVAL` | float (60.0) | Seconds between background-loop ticks. |

### `ConsolidationScheduler` interface

| Method / property | Purpose |
|---|---|
| `record_chunk(chunk=None) -> int` | Thread-safe chunk-record; bumps counter and (optionally) appends payload. Returns new count. |
| `maybe_run() -> bool` | Runs iff threshold reached; returns whether a run happened. |
| `run_once(chunks=None) -> List[ConflictAction]` | Force-run on given chunks (or pending queue). |
| `start()` / `stop(timeout=5.0)` | Daemon-thread lifecycle (idempotent start, clean shutdown). |
| `state` (property) | Snapshot copy of `ConsolidationState`. |
| `pending_count` (property) | Size of the pending-chunk queue. |
| `is_running` (property) | Whether the background thread is alive. |

### Persistence model

- `state_path="/var/lib/cortex/scheduler.json"` (default `None`) ⇒
  atomic JSON file (`os.replace` after writing `.tmp`).
- `state_io=(load_fn, save_fn)` ⇒ injectable (used by tests for
  in-memory persistence; production uses the default file I/O).
- State is loaded once in `__init__` and written on every mutation.
  Loss of the file is non-fatal: the loader falls back to defaults and
  logs at `WARNING`.

### Thread safety

- `threading.RLock` guards `_state`, `_pending`, and the background
  thread handle. RLock (not Lock) because `_loop` can call `maybe_run`
  which, on failure, may re-queue pending under the lock.
- `record_chunk` is the contended fast path: it does no I/O except the
  state-save callable (which is synchronous but cheap — JSON dump of
  six primitives).
- `run_once` / `_run_unlocked` are called WITHOUT the lock held so the
  LLM round-trip and DB writes do not block other writers.

### Design constraints honored

- **No LLM.** The scheduler itself never imports `httpx` or `tenacity`.
  The one `extract_fn` call per chunk is the only place where an LLM
  could be reached, and tests inject a synchronous stub.
- **No external deps beyond stdlib.** `json`, `os`, `secrets`,
  `threading`, `time`, plus the in-package `conflict_resolve` /
  `state` modules.
- **Fail-soft.** `_run_unlocked` catches every exception from
  `extract_fn` and `resolve_conflicts_fn`, logs at `WARNING`, and
  continues. Pending chunks are re-queued on failure so no work is
  silently dropped.
- **Schema-additive.** Scheduler never touches SQL. The
  `memory_conflict_log` audit trail is the existing #A.2 contract;
  the scheduler just calls it.
- **Test boundary.** `#A.1 extract.py` and `#A.2 conflict_resolve.py`
  are left untouched. The scheduler imports from them but does not
  modify them.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/consolidate/scheduler.py` | new | The scheduler implementation (see above). |
| `server/src/consolidate/state.py` | new | `ConsolidationState` dataclass. |
| `server/src/consolidate/__init__.py` | updated | Re-exports `ConsolidationScheduler`, `ConsolidationState`, `DEFAULT_THRESHOLD`, `DEFAULT_POLL_INTERVAL`. |
| `server/src/consolidate/CHANGELOG.md` | appended | This section. |
| `server/tests/test_scheduler_v6_5.py` | new | 4 dispatch-mandated tests + helper / regression tests. |
| `.sprint/v6.5.0-phase-1-direction-A3-report.md` | new | Sprint completion report. |

### Files **NOT** touched (per dispatch constraint)

- `server/src/consolidate/extract.py` (owned by #A.1).
- `server/src/consolidate/conflict_resolve.py` (owned by #A.2).
- `server/src/api_pg.py` (production wiring is a follow-up; the
  scheduler accepts injected `db_query` / `db_execute` so it does not
  need to import from `api_pg`).
- `server/docker/unified/schema.sql` (no schema changes needed).

### Test coverage

The 4 required tests map to the dispatch list as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_scheduler_triggers_at_threshold` | `maybe_run` fires once `chunks_since_last_run >= threshold`. |
| 2 | `test_scheduler_skips_below_threshold` | `maybe_run` returns False when count < threshold; counter untouched. |
| 3 | `test_scheduler_persists_state` | State survives a fresh `ConsolidationScheduler` reading the same `state_io` / `state_path`. |
| 4 | `test_scheduler_thread_safe` | N concurrent `record_chunk` calls on M threads yield `chunks_since_last_run == N` exactly. |

Plus helper / regression tests for `ConsolidationState.to_dict` /
`from_dict` round-trip, `run_once` with injected stubbed extract /
resolve, `start` / `stop` idempotency, and `state_io` injection.

### Open follow-ups (not in this drop)

- **#A.4 eval**: compare LOCOMO 1986 QA before/after consolidation.
- **Production wiring**: wire `api_pg.query` / `api_pg.execute` into
  the scheduler singleton at server startup; pass
  `cfg=CortexConfig()` for real LLM extraction.
- **24h cumulative trigger** (currently the scheduler only watches the
  rolling-window counter; the time-based trigger can be added as a
  second counter inside `ConsolidationState` without API churn).
- **Schema bump** (future, non-blocking): add `is_deprecated` /
  `deprecated_at` columns to `memory_extracted_facts` so the
  SUPERSEDE side-effect becomes observable to read paths. The
  scheduler already calls into the conflict_resolve path that writes
  the audit log row.

## 2026-08-23 -- v6.5.0 Phase 1 direction #C (`compact.py` + `summary.py`)

**Step 4 of 4 in the consolidation pipeline.** Long-conversation compaction
replaces the user-disabled Anthropic Memory Tool. Given a long stream of memory
chunks and a token budget, return a single concise summary row in the existing
`memory_compressed` table (no new schema).

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `compact_long_conversation(chunks, target_tokens, *, summarize_fn, db_execute, db_query, template, importance_threshold, source_chunk_id, now)` | function | Main entry -- returns a `CompactionResult`. |
| `CompactionResult` | dataclass | summary, summary_tokens, kept/dropped/skipped ids, key_facts_preserved, persistence status, decided_at. |
| `summarize_chunks(chunks, *, summarize_fn, template)` | function | LLM-friendly chunk summarizer. Returns a `str`. Never raises. |
| `DEFAULT_IMPORTANCE_THRESHOLD` | float (6.0) | Importance cutoff for "key facts". |
| `CHARS_PER_TOKEN` | int (4) | Token heuristic shared with `compact.py`. |
| `DEFAULT_TEMPLATE` | str | Deterministic summary template used when no `summarize_fn`. |

### Algorithm (5 steps)

1. **Normalize** -- coerce each chunk into `{id, content, importance}`; whitespace-only
   content and non-dict inputs are routed to `skipped_chunk_ids`.
2. **Dedupe** -- canonical content keys collide (highest importance wins). A sliding
   16-byte overlap fingerprint catches near-duplicates with trailing metadata.
3. **Budget cut** -- importance desc; key facts (importance >= gate) survive the
   budget unconditionally; flex items fill the remainder.
4. **Summarize** -- delegated to `summarize_chunks` (default template is
   deterministic, so re-runs on the same input produce the same summary string).
5. **Persist (optional)** -- one INSERT into `memory_compressed` with `RETURNING id`.
   When `db_query` is also provided, an existence check turns repeated calls into
   no-ops (idempotency contract).

### Persistence model

- Schema-additive: `memory_compressed` already exists in `schema.sql:155`.
  No migration, no DDL.
- One row per compaction. `source_chunk_id` is the highest-importance kept chunk id
  (or the explicit override / synthetic `compacted:<user_id|anon>` fallback).
- `importance` column is the max importance of the kept set.
- `created_at` defaults server-side via `NOW()`.
- Idempotency: a second call with `db_query` provided detects the existing row and
  returns `persisted=True` without a duplicate INSERT.

### Design constraints honored

- **No LLM in defaults.** The default `summarize_fn` path uses a deterministic template.
  Production wraps an LLM and injects it.
- **Pure logic.** Both files import only stdlib + the in-package `summary` helper.
  No httpx, no tenacity, no live DB required to run the unit tests.
- **Idempotent.** Same `chunks` + same `target_tokens` yields the same summary string,
  same kept/dropped partition, and (when `db_query` is wired) no duplicate DB rows.
- **Fail-soft.** DB errors are caught and logged; the caller still receives a fully
  populated `CompactionResult` with `persisted=False` and a `persisted_skipped_reason`.
- **Test boundary.** `#A.1 extract.py`, `#A.2 conflict_resolve.py`, and `#A.3`
  `scheduler.py` / `state.py` are left untouched.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/consolidate/compact.py` | new | Main compaction engine. |
| `server/src/consolidate/summary.py` | new | LLM-friendly summarizer helper. |
| `server/src/consolidate/__init__.py` | updated | Re-exports `compact_long_conversation`, `CompactionResult`, `summarize_chunks`, etc. |
| `server/src/consolidate/CHANGELOG.md` | appended | This section. |
| `server/tests/test_compact_v6_5.py` | new | 4 dispatch-mandated tests + 17 helpers + 4 unittest mirror (26 total). |
| `.sprint/v6.5.0-phase-1-direction-C-report.md` | new | Sprint completion report. |

### Files NOT touched (per dispatch scope constraint)

- `server/src/consolidate/extract.py` (#A.1).
- `server/src/consolidate/conflict_resolve.py` (#A.2).
- `server/src/consolidate/scheduler.py` / `state.py` (#A.3).
- `server/src/api_pg.py` (production wiring is a follow-up; the compactor accepts injected
  `db_query` / `db_execute` so it does not need to import from `api_pg`).
- `server/docker/unified/schema.sql` (no schema migration needed; `memory_compressed`
  already exists).

### Test coverage

The 4 required tests map to the dispatch list as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_compact_preserves_key_facts` | importance >= threshold chunks survive the budget cut. |
| 2 | `test_compact_drops_redundant_chunks` | exact-key duplicate collision keeps the highest-importance copy. |
| 3 | `test_compact_respects_target_tokens` | `summary_tokens <= target_tokens` (heuristic). |
| 4 | `test_compact_idempotent` | two calls on the same input -> same summary, same kept/dropped partition. |

Plus 17 helper / regression tests (4 summarize basics, 4 edge cases, 4 persistence,
4 dedup/source, 2 public surface, 4 unittest mirrors).

### Open follow-ups (not in this drop)

- **Production wiring**: at session-end boundary, call `compact_long_conversation(
  session_chunks, target_tokens=2000, db_execute=api_pg.execute, db_query=api_pg.query,
  summarize_fn=llm_summarize_fn)` and surface the result via a `GET /sessions/{id}/
  summary` endpoint.
- **Rolling mini-summary**: every 20 turns, call `compact_long_conversation` with the
  latest 30 chunks to keep the working set bounded without losing signal.
- **Schema bump** (future, non-blocking): add a `summarized_from_chunk_ids JSONB`
  column to `memory_compressed` so the dropped-id list becomes queryable.
- **Token-budget feedback loop**: re-compress when `summary_tokens / target_tokens > 0.95`
  by tightening `importance_threshold` (drop moderate-importance chunks).


---

## 2026-08-23 — v6.5.0 Phase 2 方向 #NEW3 (`quality.py`)

Companion to the Phase 1 #A.1 → #A.3 consolidation pipeline. Pure-logic
quality classifier that walks the fact list emitted by `extract.py` and
reports how many of the LLM-extracted facts fall below the operator's
"trust this auto-extracted fact" confidence threshold.

### Trigger conditions (per dispatch spec)

| Condition | Behavior |
|---|---|
| Input list is `None` or empty | Zeroed result (`low_quality_count=0`, `avg_confidence=0.0`, `low_quality_ratio=0.0`, `needs_review_ids=[]`). |
| Fact is not a dict | Silently skipped (mirrors `extract.py` tolerance for malformed payloads). |
| `confidence` is missing / non-numeric / out-of-range / `bool` | Fact skipped (not counted in either bucket). |
| `confidence` is a numeric float in `[0, 1]` | Counted. If `< low_quality_threshold`, the fact id is appended to `needs_review_ids`. |

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `FactQualityMonitor` | class | Configurable monitor; `low_quality_threshold` is a constructor argument (default `0.5`). |
| `FactQualityMonitor.analyze_confidence_distribution(facts)` | method | Returns the 4-key summary dict. |
| `FactQualityMonitor.is_low_quality(fact)` | method | One-off boolean check for UI / pre-validation. |
| `analyze_facts(facts, *, low_quality_threshold=0.5)` | function | Functional wrapper; equivalent to `FactQualityMonitor(...).analyze_confidence_distribution(...)`. |
| `DEFAULT_LOW_QUALITY_THRESHOLD` | float (`0.5`) | Module-level default. |

### Output contract (`analyze_confidence_distribution`)

```python
{
    "low_quality_count":  int,    # facts with confidence < threshold
    "avg_confidence":     float,  # mean of *valid* confidences; 0.0 if none
    "low_quality_ratio":  float,  # low_quality_count / total_valid; 0.0 if none
    "needs_review_ids":   list,   # ids of low-quality facts, input order
}
```

### `needs_review_ids` resolution

Stable identifier, in priority order:

1. `fact["id"]` if it is a non-empty string/int (e.g. `memory_extracted_facts.id`).
2. `f"{entity}::{predicate}"` (the canonical bucket key used by `#A.2
   conflict_resolve.py::_canonical_key`) when no explicit id but both
   `entity` and `predicate` are populated.
3. `f"fact#{fallback_index}"` — last-resort positional handle so the
   list is never shorter than `low_quality_count`.

### Design constraints honored

- **No LLM.** This module never imports `httpx` or `tenacity`. It is a
  pure analyzer over already-extracted facts.
- **No DB.** No SQL, no Postgres dependency. Tests run without a live
  database.
- **No external deps beyond stdlib.** Only `logging` and `typing`.
- **Idempotent.** Same input list -> same output dict, byte-for-byte.
- **Pure logic.** `__slots__`-based class with two pure methods; no
  hidden I/O, no global state.
- **Test boundary.** `#A.1 extract.py`, `#A.2 conflict_resolve.py`,
  `#A.3 scheduler.py` / `state.py`, and `#C compact.py` / `summary.py`
  are untouched. `quality.py` only consumes their public output shape.
- **Bool / type safety.** `bool` is rejected as a confidence value
  (would otherwise silently mean `0.0` / `1.0`). Threshold outside
  `[0, 1]` raises `ValueError` at construction.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/consolidate/quality.py` | new | The monitor implementation (see above). |
| `server/src/consolidate/__init__.py` | updated | Re-exports `FactQualityMonitor`, `analyze_facts`, `DEFAULT_LOW_QUALITY_THRESHOLD`. |
| `server/src/consolidate/CHANGELOG.md` | appended | This section. |
| `server/tests/test_quality_v6_5.py` | new | 3 dispatch-mandated tests + helper / regression tests. |
| `.sprint/v6.5.0-phase-2-direction-NEW3-report.md` | new | Sprint completion report. |

### Files **NOT** touched (per dispatch constraint)

- `server/src/consolidate/extract.py` (#A.1).
- `server/src/consolidate/conflict_resolve.py` (#A.2).
- `server/src/consolidate/scheduler.py` / `state.py` (#A.3).
- `server/src/consolidate/compact.py` / `summary.py` (#C).
- `server/src/api_pg.py` (production wiring is a follow-up; the monitor
  accepts raw dicts so it does not need to import from `api_pg`).
- `server/docker/unified/schema.sql` (no schema migration needed).

### Test coverage

The 3 required tests map to the dispatch list as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_quality_monitor_marks_low_confidence` | Facts with `confidence < threshold` (default 0.5) end up in `needs_review_ids`; higher-confidence facts do not. |
| 2 | `test_quality_monitor_threshold_configurable` | `FactQualityMonitor(threshold=0.9)` reclassifies a 0.8-confidence fact as low-quality that the default would have accepted. |
| 3 | `test_quality_monitor_returns_review_ids` | `needs_review_ids` is a list (not None / dict), populated in input order, and respects the canonical id-resolution order. |

Plus helper / regression tests for: empty / `None` input, all-malformed
input (no valid confidences), ratio math, ordering preservation, type
guarding (`bool`, non-numeric, out-of-range), threshold constructor
validation, `is_low_quality` convenience method, and module public
surface.

### Open follow-ups (not in this drop)

- **Persistence (optional)**: write `needs_review_ids` + the
  distribution snapshot into a `memory_fact_quality_log` table so
  reviewers can pull a daily digest. Schema-additive; would not touch
  any existing module.
- **LOCOMO hookup (#4)**: feed the monitor's `avg_confidence` and
  `low_quality_ratio` into the LOCOMO evaluator so quality regressions
  show up in CI before they hit production.
- **Auto-quarantine**: if `low_quality_ratio > 0.5` for a session,
  gate downstream `resolve_conflicts` on the human-reviewed subset.


---

## 2026-08-23 — v6.5.0 Phase 2 方向 #9 (`raptor.py` + `raptor_query.py`)

**RAPTOR 递归摘要层.** Multi-layer recursive summary tree over a flat
chunk list. Follows the spirit of the RAPTOR paper (Sarthi et al., 2024)
without the UMAP / GMM prerequisites -- this implementation uses
`sklearn.cluster.AgglomerativeClustering` with cosine distance +
average linkage and a deterministic ``random_state`` so re-runs on the
same input produce the same tree.

### Module surface

| Symbol | Kind | Description |
|---|---|---|
| `RaptorTree` | dataclass | The full tree: `nodes`, `root_id`, `leaf_ids`, `leaf_to_path`, `chunk_id_to_leaf`, `skipped_chunk_ids`, `max_depth`, `cluster_threshold`, `built_at`. |
| `RaptorNode` | dataclass | One node: `node_id`, `text`, `embedding`, `depth`, `children`, `chunk_ids`, `is_leaf`. |
| `build_tree(chunks, *, max_depth=3, cluster_threshold=0.7, summarize_fn=None, random_state=1729)` | function | Build the tree; always returns exactly one root. |
| `query_with_raptor(query_embedding, raptor_tree, *, top_k=10, beam=1, score_fn=None)` | function | Top-down traverse; returns ranked leaf context. |
| `DEFAULT_MAX_DEPTH` | int (`3`) | Default clustering depth. |
| `DEFAULT_CLUSTER_THRESHOLD` | float (`0.7`) | Default cosine-similarity cutoff. |
| `DEFAULT_RANDOM_STATE` | int (`1729`) | Deterministic seed (currently reserved for future stochastic extensions). |
| `DEFAULT_TOP_K` | int (`10`) | Default number of leaf results returned by the query path. |
| `DEFAULT_BEAM` | int (`1`) | Default beam width per level. |

### Tree shape invariant

* Leaves are at depth `0` (one node per chunk that survived normalization).
* The root sits at `depth == max_depth`.
* `leaf_to_path[leaf_id]` is `[root_id, ..., leaf_id]` (inclusive on both ends).
* `chunk_id_to_leaf[chunk_id]` points to the leaf that carries that chunk.

### Algorithm

1. **Normalize** chunks to `{id, text, embedding}`. Skip empty / non-dict entries;
   de-dup by `id` (last-write-wins; prior copies go to `skipped_chunk_ids`).
2. **Layer 0**: one leaf per chunk; embedding is the input vector L2-normalized.
3. **Cluster + summarize loop** (up to `max_depth` rounds):
   - Compute pairwise cosine similarity.
   - `AgglomerativeClustering(distance_threshold=1 - cluster_threshold)` buckets the layer.
   - For each cluster: invoke `summarize_fn(texts)` (or fall back to the deterministic
     template from `consolidate.summary`); embedding is the L2-normalized mean of the
     cluster's child embeddings.
   - Collapse to root the moment only one node remains.
4. If the loop exhausts `max_depth` rounds without collapsing to a single node, a
   synthetic `root` is created that summarizes the entire top layer.

### Query path

`query_with_raptor` walks the tree top-down:

1. L2-normalize `query_embedding`.
2. At each internal node, rank the children by cosine similarity (or `score_fn`)
   and descend into the top-`beam` branches.
3. The cumulative `path_score` is the sum of per-node similarities, so leaves
   under higher-similarity branches rank above leaves under lower-similarity branches
   even when their leaf embeddings coincide.
4. Returns `List[dict]` with `chunk_id`, `node_id`, `text`, `score`, `path`.
5. Empty tree / `top_k <= 0` / zero-vector query return `[]` or a zero-score
   input-order fallback -- never raise.

### Design constraints honored

- **No LLM.** `summarize_fn` is injected; the default fallback is the
  deterministic `DEFAULT_TEMPLATE` from `consolidate.summary`. Tests inject a
  trivial stub.
- **No DB.** Pure logic over the input list. No SQL, no Postgres.
- **Idempotent.** Same `chunks` + same `summarize_fn` + same `cluster_threshold`
  -> same tree (same node ids, same embeddings, same `leaf_to_path`). Verified
  by `test_raptor_idempotent`.
- **Fail-soft.** A `summarize_fn` that raises is caught; the deterministic
  fallback is used. A `score_fn` that raises returns `-inf` for that node so
  traversal skips it.
- **sklearn optional.** When sklearn is unavailable, a deterministic greedy
  single-linkage fallback produces equivalent results modulo tie order.
- **Deterministic sort keys.** `_relabel` re-maps sklearn cluster ids to
  `0..k-1` in input-order of first appearance. Member buckets are sorted by
  `node_id`. The chunk list is sorted by id (empty ids sink to the end) before
  leaves are built.
- **Single-root invariant.** Either the clustering loop collapses to one node
  or a synthetic `root` summarizes the top layer; the public contract always
  exposes exactly one root.
- **Test boundary.** `extract.py` / `conflict_resolve.py` / `scheduler.py` /
  `state.py` / `compact.py` / `summary.py` / `quality.py` are untouched.
  `raptor.py` imports only from stdlib + numpy + sklearn + the in-package
  `summary` helper.

### Files added / changed in this drop

| File | Status | Purpose |
|---|---|---|
| `server/src/consolidate/raptor.py` | new | `RaptorTree`, `RaptorNode`, `build_tree`, helpers. |
| `server/src/consolidate/raptor_query.py` | new | `query_with_raptor`, helpers, score-fn injection. |
| `server/src/consolidate/__init__.py` | updated | Re-exports the #9 surface. |
| `server/src/consolidate/CHANGELOG.md` | appended | This section. |
| `server/tests/test_raptor_v6_5.py` | new | 5 dispatch-mandated tests + helper / regression tests. |
| `.sprint/v6.5.0-phase-2-direction-9-report.md` | new | Sprint completion report. |

### Files **NOT** touched (per dispatch constraint)

- `server/src/consolidate/extract.py` (#A.1).
- `server/src/consolidate/conflict_resolve.py` (#A.2).
- `server/src/consolidate/scheduler.py` / `state.py` (#A.3).
- `server/src/consolidate/compact.py` / `summary.py` (#C).
- `server/src/consolidate/quality.py` (#NEW3).
- `server/src/api_pg.py` (production wiring is a follow-up; both modules accept
  injected callables so they do not need to import from `api_pg`).
- `server/docker/unified/schema.sql` (no schema migration needed; RAPTOR is a
  pure in-memory index over existing chunks).

### Test coverage

The 5 required tests map to the dispatch list as follows:

| # | Test | Spec point exercised |
|---|---|---|
| 1 | `test_build_tree_clusters_similar_chunks` | Chunks with similar embeddings land in the same cluster node. |
| 2 | `test_tree_respects_max_depth` | `max_depth=1` builds a root + leaves only; `max_depth=3` can produce a 4-layer tree (depths 0..3). |
| 3 | `test_query_traverses_tree_top_down` | The returned `path` for every hit includes the root id as the first element and the leaf id as the last. |
| 4 | `test_query_collects_relevant_leaves` | With `beam>1`, leaves that share the most-similar branch outrank leaves under low-similarity branches. |
| 5 | `test_raptor_idempotent` | Two `build_tree` calls on the same input produce structurally identical trees. |

Plus helper / regression tests for: empty input, single-chunk input,
chunks without embeddings (zero-vector fallback), custom `summarize_fn`,
custom `score_fn`, top-k truncation, zero-vector query, non-RaptorTree
input (TypeError), `path_for_chunk`, module public surface, cluster
threshold clamping, `RaptorNode`/`RaptorTree` dataclass field accessors.

### Open follow-ups (not in this drop)

- **Production wiring**: at session-end boundary, call `build_tree(
  session_chunks, summarize_fn=llm_summarize_fn)` and store the result
  alongside `memory_compressed`. The tree itself can be cached in memory
  or serialized to JSON (the dataclasses are JSON-friendly).
- **Recall hook**: add a `/recall/raptor` endpoint that wraps
  `query_with_raptor` with the live embedder.
- **LOCOMO hookup (#4)**: compare retrieval recall@k before/after
  RAPTOR-augmented query expansion.
- **Optional UMAP**: if/when umap-learn is added to the dependency
  tree, swap the row vectors for UMAP-reduced embeddings before
  clustering (the original RAPTOR recipe).
