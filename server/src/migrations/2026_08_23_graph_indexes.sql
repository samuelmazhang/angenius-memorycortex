-- Migration: V6.5.0 Phase 1 direction #B graph indexes + tables.
-- Date: 2026-08-23
-- Author: claude-code (auto-pilot sprint 6/7)
--
-- This migration is purely additive:
--   * Adds 4 composite (btree) indexes on memory_relations + memory_links
--     for the Personalized PageRank walk.
--   * Adds 2 new tables: memory_graph_communities + memory_graph_ppr
--     (the community-detection and PPR result caches).
--
-- Safe to re-run: every CREATE uses IF NOT EXISTS.

-- Composite covering indexes for PPR walk on memory_relations
CREATE INDEX IF NOT EXISTS memory_relations_m1_strength_idx
    ON memory_relations(memory_id_1, strength);
CREATE INDEX IF NOT EXISTS memory_relations_m2_strength_idx
    ON memory_relations(memory_id_2, strength);

-- Composite covering indexes for PPR walk on memory_links
CREATE INDEX IF NOT EXISTS memory_links_src_strength_idx
    ON memory_links(source_id, strength);
CREATE INDEX IF NOT EXISTS memory_links_tgt_strength_idx
    ON memory_links(target_id, strength);

-- Community detection cache (Leiden / Louvain / label-propagation)
CREATE TABLE IF NOT EXISTS memory_graph_communities (
    community_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'leiden',
    partition_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (algorithm, community_id, member_id)
);
CREATE INDEX IF NOT EXISTS memory_graph_communities_member_idx
    ON memory_graph_communities(member_id, algorithm);

-- PPR result cache
CREATE TABLE IF NOT EXISTS memory_graph_ppr (
    anchor_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    score REAL NOT NULL,
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (anchor_id, node_id)
);
CREATE INDEX IF NOT EXISTS memory_graph_ppr_computed_at_idx
    ON memory_graph_ppr(computed_at DESC);
CREATE INDEX IF NOT EXISTS memory_graph_ppr_anchor_computed_idx
    ON memory_graph_ppr(anchor_id, computed_at DESC);