-- Migration: v6.5.0 Phase 1 direction #3 — Nomic v1.5 embedding upgrade
-- Date: 2026-08-23
-- Spec: workflow-graph/tasks/v6.5.0-phase-1-implementation/spec.md (方向 #3)
--
-- Adds `model_version` column to memory_chunks for embedding model tracking.
-- Existing chunks default to 'mpnet' (paraphrase-multilingual-MiniLM-L12-v2);
-- ETL background job (server/src/etl_reembed.py) re-embeds them with
-- 'nomic-v1.5' incrementally (10% / 50% / 100% A/B rollout, spec §6.2).
--
-- Column dimension strategy (see decision-log.md #3 entry):
--   Pre-#3 schema:  embedding vector(384) — fits MiniLM but NOT Nomic (768).
--   Post-#3 schema: schema.sql declares embedding vector(768); mpnet vectors
--                   zero-pad to 768 dims on UPSERT, the search dispatcher
--                   filters by model_version so the two spaces never mix.
--   End state:      ETL completes, all chunks on nomic-v1.5, column stays 768.

ALTER TABLE memory_chunks
    ADD COLUMN IF NOT EXISTS model_version VARCHAR(32) DEFAULT 'mpnet' NOT NULL;

-- Index: helps /search dispatch select the right embedding model by chunk version,
-- and helps ETL pick the next batch of stale chunks.
CREATE INDEX IF NOT EXISTS memory_chunks_model_version_idx
    ON memory_chunks(model_version)
    WHERE archived = 0;

-- Backfill safety: any chunk missing the column gets 'mpnet' (idempotent).
UPDATE memory_chunks
    SET model_version = 'mpnet'
    WHERE model_version IS NULL;

-- NOTE: column widening from vector(384) to vector(768) is performed by the
-- schema.sql CREATE TABLE on fresh installs. Existing prod databases need a
-- separate one-time ALTER (run during deployment window):
--   ALTER TABLE memory_chunks ALTER COLUMN embedding TYPE vector(768);
-- This is intentionally NOT bundled into this migration so batch jobs can be
-- scheduled independently.

-- Down migration (commented for reference, do not run unless rolling back):
--   ALTER TABLE memory_chunks DROP COLUMN IF EXISTS model_version;
