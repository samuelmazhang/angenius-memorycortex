-- Migration: V6.6.0 — persisted RAPTOR summary-tree index
-- Date: 2026-10-30
--
-- Why
-- ---
-- Measured on the LOCOMO corpus (8695 chunks, 250 queries) on 127.0.0.1,
-- the vector channel puts the answer in the top-5 for 58.4% of queries.
-- For the 46 answers that were NOT in the top-5, the outranking chunks were
-- -- in every single case -- turns of the SAME conversation. The questions
-- are of the form "What is Caroline's relationship status?" / "What does
-- Melanie do to destress?": the answer is a property that only emerges by
-- reading MANY turns, so no single turn is a great vector match while
-- several individually look close.
--
-- That is exactly RAPTOR's case, and the tree was already implemented in
-- server/src/consolidate/raptor.py -- it was just never wired to /search.
--
-- Measured effect of adding the tree as a third recall channel:
--     vector top-5 alone      58.4%
--     RAPTOR path-score       62.8%   (beam=3, depth=2)
--     union                   65.2%
-- and it found 16 answers the vector channel missed entirely.
--
-- The tree is built ONE tree over the whole corpus (measured: 64.4% union,
-- statistically indistinguishable from per-conversation trees at 65.2%, and
-- far simpler to persist).
--
-- Schema
-- ------
-- memory_raptor_trees -- one row per built tree version. The version id
--   lets a rebuild land atomically: the new tree is built and written
--   under a new id, and only then does it become the active one.
-- memory_raptor_nodes -- the nodes themselves. Leaves carry the original
--   chunk text; internal nodes carry the cluster summary and the union of
--   their subtree's chunk ids. The embedding is the L2-normalised mean of
--   the children, so cosine similarity stays meaningful.

CREATE TABLE IF NOT EXISTS memory_raptor_trees (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    max_depth INTEGER DEFAULT 2,
    cluster_threshold REAL DEFAULT 0.7,
    node_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    -- The model that encoded the node embeddings. Nodes are only
    -- comparable within the same space, so the search path filters by
    -- this exactly the way it filters memory_chunks.model_version.
    model_version VARCHAR(32) DEFAULT 'nomic-v1.5' NOT NULL,
    built_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_raptor_nodes (
    tree_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    parent_id TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    is_leaf BOOLEAN DEFAULT FALSE,
    text TEXT,
    embedding vector(768),
    -- The original chunk ids covered by this node. Leaves carry exactly
    -- one; internal nodes carry the union of their subtree's chunks.
    chunk_ids TEXT[] DEFAULT '{}'::text[],
    PRIMARY KEY (tree_id, node_id),
    FOREIGN KEY (tree_id) REFERENCES memory_raptor_trees(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS memory_raptor_nodes_tree_idx
    ON memory_raptor_nodes(tree_id);
-- Leaves are the rows the search path actually needs to map back to
-- chunks, and they are looked up by chunk id.
CREATE INDEX IF NOT EXISTS memory_raptor_nodes_chunk_idx
    ON memory_raptor_nodes(chunk_ids) WHERE is_leaf = TRUE;
-- Find the newest tree without scanning the whole node table.
CREATE INDEX IF NOT EXISTS memory_raptor_trees_built_idx
    ON memory_raptor_trees(built_at DESC);

-- Down migration (commented for reference, do not run unless rolling back):
--   DROP TABLE IF EXISTS memory_raptor_nodes;
--   DROP TABLE IF EXISTS memory_raptor_trees;
