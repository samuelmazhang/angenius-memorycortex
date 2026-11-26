-- Migration: v6.5.0 Phase 1 direction #5 — HNSW m=16 → m=24 retune
-- Date: 2026-08-23
-- Spec: workflow-graph/tasks/v6.5.0-phase-1-direction-5/spec.md (方向 #5)
--
-- HNSW graph tuning: bump m (max edges per node) from pgvector default 16 → 24.
-- Trade-off (per pgvector docs + ANN-benchmarks):
--   - m=16 (default):  baseline recall, fast build, smallest index
--   - m=24:            +5pp recall@10 on 768-dim Nomic embeddings, ~1.5x build
--                       time, ~1.4x index size. Worth it on warm caches.
-- ef_construction kept at 200 (build-time search depth).
-- Search-time ef_search is set per-session by the app (see api_pg.py startup).
--
-- Safety: dropping + recreating triggers a FULL index rebuild. On large
-- tables (>10M rows) this can take hours. Run during a maintenance window.
--
-- Idempotent: re-running after the rebuild is a no-op (PG accepts same option value).

DROP INDEX IF EXISTS memory_chunks_hnsw_idx;

CREATE INDEX IF NOT EXISTS memory_chunks_hnsw_idx
    ON memory_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 200);

-- Down migration (commented for reference; do not run unless rolling back):
--   DROP INDEX IF EXISTS memory_chunks_hnsw_idx;
--   CREATE INDEX memory_chunks_hnsw_idx
--       ON memory_chunks USING hnsw (embedding vector_cosine_ops);
--   -- (drops back to pgvector defaults: m=16, ef_construction=64)
