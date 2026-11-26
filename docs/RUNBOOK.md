# Cortex Runbook

Operational reference for the cortex 6.5.1 deployment on 8.2 (127.0.0.1).
Covers: source deployment, image rebuilds, scheduler diagnostics, relation
graph health, multi-worker layout, and the script inventory.

Last updated: 2026-09-02 (after the relation-graph + image-tag + multi-worker
series of commits). All scripts referenced here are in `scripts/` and the
ADRs they implement are cross-linked below.

---

## 1. System layout (live as of 2026-09-02)

```
8.2 host (127.0.0.1)
+-- /opt/cortex-deploy/
|   +-- work/                                  git clone, bind-mount source
|   +-- cortex-deploy.sh                       hot update source (seconds)
|   +-- cortex-rebuild.sh                      rebuild :latest image (5-10 min)
+-- memory-cortex-stack:latest                 built from server/docker/unified/Dockerfile
+-- memory-cortex-data (volume)                persistent PG 16 + pgvector

Running containers
+-- memory-cortex (port 1880)                 main API
|   +-- PG internal (5432) + bind-mount /app/src (RO)
|   +-- P2 worker: scheduler enabled, is_running=true
|   +-- worker_id: 30f774426f97:1
+-- memory-cortex-maintain (port 1881)        multi-worker replica
    +-- PG via external PG_HOST=127.0.0.1 (memory-cortex IP, not volume)
    +-- worker_id: 99489c957285:1

Endpoints
+-- http://127.0.0.1:1880       main API
+-- http://127.0.0.1:1881       replica API (multi-worker pool)

Multi-worker coordination
+-- Both workers share PG queue (memory_maintenance_queue)
+-- SKIP LOCKED ensures no double-processing
+-- Container name "memory-cortex-maintain" reflects ADR-260 (multi-worker
    horizontal scale). Either port can serve requests.
```

For the image-vs-code version decoupling rule, see ADR-264.

---

## 2. Source vs image deployment

| Change type | Command | Downtime |
|---|---|---|
| Code only (api_pg.py, schedules, tests, scripts) | `git push` -> host `cortex-deploy.sh` | ~5 s (container restart) |
| Dockerfile / apt / pip / python version | `git push` -> host `cortex-rebuild.sh` | 5-10 min (rebuild image, restart) |
| Source code version bump (api_pg.py:app.version) | edit + push + deploy | same as code-only path |
| Image tag rename | `docker tag <id> <new-name>` (no rebuild) | none |

Image tag is `:latest`. Source code version lives in `VERSION` and
`api_pg.py:app.version`. Decoupled per ADR-264.

---

## 3. Scripts inventory

| Script | When | What |
|---|---|---|
| **scripts/cortex-deploy.sh** | code-only changes | git pull + apply migrations + sync venv if needed + restart container |
| **scripts/cortex-rebuild.sh** | base image / pip changes | git pull + rebuild :latest image + retag previous + restart container |
| **scripts/cortex-backfill-relations.sh** | relation graph health | enqueue every active chunk through P2 worker to regenerate similar + tag-shared relations. Idempotent (ON CONFLICT DO UPDATE) |
| **scripts/cortex-purge-test-chunks.sh** | test residue cleanup | DELETE chunks matching strict test patterns. `DRY_RUN=1` to preview |
| **scripts/cortex-diag.sh** | "why does this chunk have 0/N relations?" | 6-step diagnostic per chunk: metadata, relations, links, tag overlap, embedding similarity, P2 worker state. Use this instead of writing ad-hoc SQL |

Always run scripts on the 8.2 host, not locally.

---

## 4. Admin endpoints

All on http://127.0.0.1:1880 (also 1881):

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | version, status, chunk/relation/vector counts |
| `/stats` | GET | counts split between memory_relations + memory_links |
| `/storage/health` | GET | disk + PG + vector storage_status |
| `/endpoints` | GET | full route inventory (89 endpoints) |
| `/admin/maintenance/stats` | GET | queue counts, scheduler state, both worker_ids |
| `/admin/maintenance/dead` | GET | chunks that flipped to status=dead (failed 5+ retries) |
| `/admin/maintenance/run-once` | POST | force one worker tick |
| `/admin/maintenance/enqueue/{chunk_id}?force=true` | POST | re-enqueue a chunk into P2 worker |
| `/metrics` | GET | Prometheus text-format with 22 cortex_* metrics |
| `/chunks` | POST | create chunk + enqueue P2 + write similar + tag-shared |
| `/relations/{memory_id}` | GET | single-chunk relations (out + in) |
| `/relations/batch?ids=id1,id2&depth=1..3&max_per_node=10` | GET | multi-anchor graph walk |
| `/search` | POST | hybrid vector + lexical + tag recall |

---

## 5. P2 scheduler -- what it does automatically

Every POST /chunks:
1. `enqueue_chunk_id(cid)` appends to in-memory deque (sub-ms)
2. Flush thread (every 0.2 s) batch INSERT into `memory_maintenance_queue`
3. Worker thread (every 1.0 s, SKIP LOCKED) claims one chunk at a time
4. `_process_one(cid)`:
   - load chunk row from memory_chunks
   - extract_fn -> envelope
   - resolve_conflicts_fn -> taxonomy_boost + conflict_resolution
   - mark_done
5. `on_process_complete(cid)` fires the auto-relate hooks:
   - `_sched_record_similar_relations`: top-5 nearest neighbors via pgvector
     cosine distance, threshold sim >= 0.65. Writes `similar` relations.
   - `_sched_record_tag_shared_relations`: chunks with >= 2 shared tags.
     Writes `tag-shared` relations with strength=overlap_count.

Failure modes (best-effort, log + swallow, never flip chunk to dead):
- chunk has no embedding -> tag-shared hook still runs, similar returns early
- vector query raises -> outer try/except in `_worker_tick` logs + skips hook
- zero neighbors above threshold -> return early
- individual INSERT fails -> per-row try/except, continue with next

Retry: 5 attempts with exponential backoff (30s, 60s, 120s, ...). After 5,
status flips to `dead`. See ADR-260 for multi-worker design rationale.

---

## 6. How to diagnose "this chunk should have relations but doesn't"

```bash
# 1. Run the diag script -- pre-built safe SQL, no manual queries
ssh <user>@127.0.0.1 \
  "bash /opt/cortex-deploy/work/scripts/cortex-diag.sh <chunk_id>"

# Output:
#   step 1: chunk metadata (id, type, tags, embedding presence)
#   step 2: memory_relations involving this chunk (both directions)
#   step 3: memory_links involving this chunk (both directions)
#   step 4: tag overlap analysis (top 10 with overlap count)
#   step 5: top-5 embedding-similar chunks with cosine_sim
#   step 6: P2 worker state on both port 1880 and 1881
#   + heuristic hints at the bottom

# 2. If a hook missed a real match, force-enqueue:
curl -X POST http://127.0.0.1:1880/admin/maintenance/enqueue/<chunk_id>?force=true

# 3. If you need to regenerate relations for the historical corpus:
ssh <user>@127.0.0.1 \
  "bash /opt/cortex-deploy/work/scripts/cortex-backfill-relations.sh"
```

**Do NOT write ad-hoc inline SQL.** It tends to hit ambiguous column
references and JSONB cast errors (see ADR-265). The diag script is the
official interface.

---

## 7. Multi-worker layout (ADR-260)

Two containers share the same PG queue:

- `memory-cortex` (port 1880) is the original; PG runs inside the container
  and is reachable via docker bridge at `127.0.0.1:5432`.
- `memory-cortex-maintain` (port 1881) connects to that PG via
  `PG_HOST=127.0.0.1`. We override the container's entrypoint with
  `bash -c "cd /app && exec python3 -m src.api_pg"` so it does not
  start its own PG (which would conflict on port 5432 inside the
  container).

To add a third worker:
```bash
sudo docker run -d --name memory-cortex-maintain-2 --restart unless-stopped \
  -p 1882:1880 \
  -v /opt/cortex-deploy/work/server/src:/app/src:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/schema.sql:/opt/cortex/schema.sql:ro \
  -v /opt/cortex-deploy/work/server/docker/unified/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro \
  -v /root/.local/share/cortex-models:/root/.arcadia/plugins/memory-cortex/models:ro \
  --entrypoint "bash" \
  -e PG_HOST=127.0.0.1 -e PG_PORT=5432 \
  -e PG_DB=postgres -e PG_USER=postgres -e PG_PASSWORD=<pw> \
  -e EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/nomic-embed-text-v1.5 \
  memory-cortex-stack:latest \
  -c "export PATH=/opt/cortex-venv/bin:\$PATH; export VIRTUAL_ENV=/opt/cortex-venv; \
      export PYTHONPATH=/opt/cortex-venv/lib/python3.11/site-packages:\${PYTHONPATH:-}; \
      cd /app && exec python3 -m src.api_pg"
```

Each new worker registers itself in PG (worker_id = container_id:1) and
shares work via SKIP LOCKED.

---

## 8. Image tag policy (ADR-264)

- Image tag: `memory-cortex-stack:latest`. Always.
- Source code version: `api_pg.py:app.version` and top of `VERSION` file.
- Renaming an image is `docker tag <id> <new-name>`, not a rebuild.
- `cortex-rebuild.sh NEW_TAG=latest`. Don't change this.
- `Dockerfile LABEL` does not set `org.opencontainers.image.version`. It
  points operators at `/health.version` for the running version.

---

## 9. Operational health checks

Before any manual intervention, look at:

```bash
curl -s http://127.0.0.1:1880/health | python3 -m json.tool
curl -s http://127.0.0.1:1880/admin/maintenance/stats | python3 -m json.tool
curl -s http://127.0.0.1:1880/stats | python3 -m json.tool
curl -s http://127.0.0.1:1880/metrics | grep -E '^cortex_(queue|scheduler)'
```

Expected baseline:
- `/health.status = "healthy"`, `/health.version = "6.5.1"` (or current)
- `/admin/maintenance/stats.is_running = true`
- `/admin/maintenance/queue_counts.dead = 0`
- `/admin/maintenance/queue_counts.pending = 0` (steady state)
- both port 1880 and 1881 workers show `is_running=true`

If `/health.status != "healthy"`, check `docker logs memory-cortex` and
`docker logs memory-cortex-maintain` for `ERROR` / `No module named` /
`Address already in use`.

---

## 10. ADRs (Architecture Decision Records)

Listed by date. Cross-references are noted.

- ADR-052 -- Cortex takeover (the original)
- ADR-063 -- numpy ambiguous fix
- ADR-064 -- search metadata NPE
- ADR-065 -- forgetting scheduler
- ADR-066 -- forgetting persistence
- ADR-067 -- heat reinforce v6
- ADR-067-patch-1 -- search heat write
- ADR-068 -- conflict resolve
- ADR-069 -- webui v5.4 data governance
- ADR-070 -- cron alert channel
- ADR-071 -- connector deadcode cleanup
- ADR-072 -- webui e2e playwright
- ADR-073 -- v7 spreading activation POC
- ADR-074 -- spreading activation algorithm
- ADR-075 -- content-driven maintenance (the path P2 implements)
- ADR-076 -- summarize fn contract unification
- ADR-077 -- nomic v1.5 local snapshot deploy
- ADR-078 -- LOCOMO 1986 benchmark
- ADR-2026-08-23-cortex-full-takeover -- end-to-end migration
- ADR-2026-09-01-258 -- deprecate cortex-memory-maintenance.sh (replaced by P2)
- ADR-2026-09-01-260 -- multi-worker scheduler (memory-cortex-maintain)
- ADR-2026-09-01-261 -- test chunk cleanup (cortex-purge-test-chunks.sh)
- ADR-2026-09-01-262 -- real-time similar relation graph (P2 auto-relate hook)
- ADR-2026-09-01-263 -- P2 relation enhancements (0.65 threshold, tag-shared, batch endpoint)
- ADR-2026-09-02-264 -- image tag policy (:latest)
- ADR-2026-09-02-265 -- diag SOP (cortex-diag.sh)

ADRs are git-tracked, append-only. When you change the system, write a
new ADR. Do not edit old ones except for typo fixes.
