-- Memory Cortex V5.2 unified schema
-- Auto-applied by entrypoint on first run

-- Required extensions
CREATE EXTENSION IF NOT EXISTS vector;

-- Memory chunks (main table)
CREATE TABLE IF NOT EXISTS memory_chunks (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT DEFAULT 'general',
    tags JSONB DEFAULT '[]'::jsonb,
    source TEXT DEFAULT 'user',
    importance INTEGER DEFAULT 5,
    metadata JSONB DEFAULT '{}'::jsonb,
    -- V6.5.0 Phase 1 direction #3: embedding widened from vector(384) to
    -- vector(768) to fit nomic-ai/nomic-embed-text-v1.5. Mpnet (384-dim)
    -- chunks zero-pad to 768 on UPSERT; search dispatcher filters by
    -- model_version so the two spaces are never compared. Existing prod
    -- databases need a one-time ALTER (see migrations/2026_08_23_*).
    embedding vector(768),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    tier TEXT DEFAULT 'short_term',
    expires_at TIMESTAMPTZ,
    archived INTEGER DEFAULT 0,
    -- V5.4 conflict-resolve: 按 (user_id, source_key) 精确分桶,新事实可覆盖旧事实
    -- user_id 来源 metadata->>'user_id' (由调用方写入), source_key 由调用方提供
    -- 旧记录两个字段均为 NULL,不参与去重,行为完全兼容 V5.3
    is_deprecated BOOLEAN DEFAULT FALSE,
    deprecated_at TIMESTAMPTZ,
    deprecated_by TEXT,         -- 触发 supersede 的新 chunk id
    superseded_by TEXT,         -- 反向: 谁覆盖了我
    -- V6.5.0 Phase 1 direction #3 (2026-08-23): embedding model tag.
    -- 'mpnet' = MiniLM-L12-v2 (legacy, 384-dim).
    -- 'nomic-v1.5' = nomic-ai/nomic-embed-text-v1.5 (MTEB 62.28, default for new chunks).
    -- The ETL background job (server/src/etl/etl_reembed.py) flips this column
    -- incrementally (10% / 50% / 100% A/B rollout) and rewrites the embedding.
    model_version VARCHAR(32) DEFAULT 'mpnet' NOT NULL,
    -- V6.6.0 (2026-10-30): origin binding.
    --
    -- Agent memory is a persistent attack channel: measured 2026, a
    -- malicious instruction planted via one untrusted tool call fires in
    -- later sessions with 55.5% success against OpenClaw and 81.7% against
    -- Claude Code, and one benchmark's sleeper implant still fired after
    -- 100 ordinary sessions. Prompt-level filters cut poisoning by at most
    -- 3.2 points; what actually works is binding the origin to the record
    -- AT WRITE TIME (Origin-Bound Authority: attack success 0%).
    --
    -- origin      — where the fact came from ('user', 'agent', 'tool',
    --               'external', ...). Set once, at write time.
    -- origin_trust— 'user' (directly asserted by the human) |
    --               'derived' (extracted by the agent from user input) |
    --               'external' (came from outside the trust boundary:
    --               web, tool output, another agent).
    -- origin_ref  — the concrete provenance handle (url, message id,
    --               tool call id) so a claim can be traced back.
    --
    -- The marker must TRAVEL with the record: consolidation that rewrites a
    -- fact must not silently upgrade its trust, or provenance laundering
    -- becomes possible (an external observation rewritten as if it were the
    -- user's own history).
    origin VARCHAR(32) DEFAULT 'agent',
    origin_trust VARCHAR(16) DEFAULT 'derived',
    origin_ref TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);
-- Provenance lookups: "show me everything that came from outside the trust
-- boundary" and "audit one fact's origin".
CREATE INDEX IF NOT EXISTS memory_chunks_origin_idx
    ON memory_chunks(origin_trust) WHERE archived = 0;
-- V6.5.0 Phase 1 direction #5 (2026-08-23): HNSW graph widened from m=16 to m=24.
-- Higher m = more edges per node = better recall@10 (~+5pp on LOCOMO), at the cost
-- of ~1.5x index build time + ~1.4x index size. ef_construction=200 keeps build
-- quality high. Production databases need ALTER INDEX with (m=24, ef_construction=200)
-- (see migrations/2026_08_23_hnsw_m24.sql). Search-time ef_search is set per-session
-- via `SET hnsw.ef_search = 120` at app startup (server/src/api_pg.py).
CREATE INDEX IF NOT EXISTS memory_chunks_hnsw_idx ON memory_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 24, ef_construction = 200);
CREATE INDEX IF NOT EXISTS memory_chunks_model_version_idx
    ON memory_chunks(model_version)
    WHERE archived = 0;
CREATE INDEX IF NOT EXISTS memory_chunks_tsv_idx ON memory_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS memory_chunks_type_idx ON memory_chunks(type);
CREATE INDEX IF NOT EXISTS memory_chunks_deprecated_idx ON memory_chunks(is_deprecated) WHERE is_deprecated = FALSE;
CREATE INDEX IF NOT EXISTS memory_chunks_source_key_idx
    ON memory_chunks((metadata->>'source_key')) WHERE metadata ? 'source_key';

-- V5.4 conflict audit: 记录每一次"新事实覆盖旧事实"决策,供运维/审计追溯
-- action: 'supersede' (新覆盖旧) / 'mark_only' (只打标不删) / 'reject' (新事实被拒,保留旧)
CREATE TABLE IF NOT EXISTS memory_conflict_log (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    source_key TEXT NOT NULL,
    old_chunk_id TEXT,
    new_chunk_id TEXT,
    action TEXT NOT NULL,
    reason TEXT,
    confidence REAL,            -- 新事实的置信度 0-1
    decided_by TEXT DEFAULT 'auto',  -- 'auto' (cortex 决策) / 'manual' (运维手动)
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_conflict_log_user_idx
    ON memory_conflict_log(user_id, source_key, created_at DESC);
CREATE INDEX IF NOT EXISTS memory_conflict_log_action_idx
    ON memory_conflict_log(action, created_at DESC);

-- Memory relations
CREATE TABLE IF NOT EXISTS memory_relations (
    id TEXT PRIMARY KEY,
    memory_id_1 TEXT NOT NULL,
    memory_id_2 TEXT NOT NULL,
    co_occurrence_count INTEGER DEFAULT 1,
    relation_type TEXT DEFAULT 'implicit',
    strength REAL DEFAULT 0.0,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    episode_id TEXT,
    transaction_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS memory_relations_m1_idx ON memory_relations(memory_id_1);
CREATE INDEX IF NOT EXISTS memory_relations_m2_idx ON memory_relations(memory_id_2);
-- FIX (2026-08-02): /merge-duplicates used ON CONFLICT (memory_id_1, memory_id_2)
-- without a matching unique constraint -> 500 every call. Add it now.
CREATE UNIQUE INDEX IF NOT EXISTS memory_relations_m1_m2_uniq
    ON memory_relations(memory_id_1, memory_id_2);
-- V6.5.0 Phase 1 direction #B (2026-08-23): composite covering indexes for the
-- Personalized PageRank walk over `memory_relations`. The PPR engine
-- (server/src/graph/ppr.py) pulls `memory_id_1`/`memory_id_2`/`strength`
-- together, so a single composite index replaces the 2 single-column
-- lookups + a heap fetch. btree is correct here (not GIN) because the
-- queries are equality / range on a single column, not full-text JSONB.
CREATE INDEX IF NOT EXISTS memory_relations_m1_strength_idx
    ON memory_relations(memory_id_1, strength);
CREATE INDEX IF NOT EXISTS memory_relations_m2_strength_idx
    ON memory_relations(memory_id_2, strength);

-- Memory links (was missing entirely; /links/* endpoints all 500'd).
-- Connector uses these to track co-occurrence strength + recency between chunks.
CREATE TABLE IF NOT EXISTS memory_links (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    link_type TEXT DEFAULT 'related',
    strength REAL DEFAULT 0.5,
    source TEXT DEFAULT 'manual',
    access_count INTEGER DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_links_src_idx ON memory_links(source_id);
CREATE INDEX IF NOT EXISTS memory_links_tgt_idx ON memory_links(target_id);
-- V6.5.0 Phase 1 direction #B (2026-08-23): composite covering indexes for the
-- PPR walk over `memory_links`. Same rationale as memory_relations above.
CREATE INDEX IF NOT EXISTS memory_links_src_strength_idx
    ON memory_links(source_id, strength);
CREATE INDEX IF NOT EXISTS memory_links_tgt_strength_idx
    ON memory_links(target_id, strength);

-- Extracted facts (was missing; /consolidate/facts returned 500).
-- Written by POST /consolidate/extract, read by GET /consolidate/facts.
CREATE TABLE IF NOT EXISTS memory_extracted_facts (
    id BIGSERIAL PRIMARY KEY,
    source_chunk_id TEXT,
    fact TEXT NOT NULL,
    user_id TEXT,
    confidence REAL DEFAULT 0.5,
    -- V6.6.0 (2026-10-30): the origin travels with the DERIVED fact too.
    --
    -- This is the anti-laundering mechanism. A fact extracted from a
    -- source chunk must inherit that chunk's origin marker, and it may
    -- never be MORE trusted than its source: an observation that came from
    -- outside the trust boundary stays 'external' after consolidation, so
    -- a rewritten fact cannot masquerade as something the user said.
    --
    -- Without this, consolidation can launder provenance: take an external
    -- observation, rewrite it as a compact 'user preference' fact, and the
    -- result reads as if the user had always said it.
    origin VARCHAR(32) DEFAULT 'agent',
    origin_trust VARCHAR(16) DEFAULT 'derived',
    origin_ref TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_extracted_facts_created_at_idx
    ON memory_extracted_facts(created_at DESC);

-- Media (was missing; /media/search and /media/by-type returned 500).
CREATE TABLE IF NOT EXISTS memory_media (
    id TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    content TEXT,
    mime_type TEXT DEFAULT 'text/plain',
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_media_type_idx ON memory_media(media_type);

-- Resources (was missing; /resources/search and /resources/{id}/accessed returned 500).
CREATE TABLE IF NOT EXISTS memory_resources (
    id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    metadata JSONB,
    access_count INTEGER DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_resources_type_idx ON memory_resources(resource_type);

-- Compressed chunks (was missing; /compress/{source_chunk_id} returned 500).
-- Written by /compress/run when chunks are summarized for storage savings.
CREATE TABLE IF NOT EXISTS memory_compressed (
    id BIGSERIAL PRIMARY KEY,
    source_chunk_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_compressed_source_idx
    ON memory_compressed(source_chunk_id, created_at DESC);

-- Memory audit history
CREATE TABLE IF NOT EXISTS memory_history (
    id BIGINT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    content TEXT,
    type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_history_memory_id_idx ON memory_history(memory_id);

-- Working memory
CREATE TABLE IF NOT EXISTS working_memory (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, key)
);

-- Conversation turns (was missing; POST /working/{sid}/append "B path" returned
-- 501 and GET /working/{sid}/context returned a hardcoded empty conversation).
-- Direction B: persists cross-turn LLM dialogue so a session can resume its
-- context. Each row is one message turn with an ordinal for stable ordering.
CREATE TABLE IF NOT EXISTS memory_conversations (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS memory_conversations_session_idx
    ON memory_conversations(session_id, seq);

-- Core profile
CREATE TABLE IF NOT EXISTS core_profile (
    user_id TEXT PRIMARY KEY,
    profile JSONB,
    source TEXT DEFAULT 'user',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Episodes
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    summary TEXT,
    events JSONB DEFAULT '[]'::jsonb,
    participants JSONB DEFAULT '[]'::jsonb,
    duration_seconds INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Hierarchy nodes
CREATE TABLE IF NOT EXISTS hierarchy_nodes (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    type TEXT NOT NULL,
    label TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Procedural memory
CREATE TABLE IF NOT EXISTS procedural (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    steps JSONB DEFAULT '[]'::jsonb,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Access stats
CREATE TABLE IF NOT EXISTS access_stats (
    id BIGSERIAL PRIMARY KEY,
    memory_id TEXT NOT NULL,
    access_type TEXT,
    accessed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tags taxonomy (was tags_hierarchy in api_pg.py — referenced table that never existed; the real table is tags_taxonomy with columns tag/parent_tag)
CREATE TABLE IF NOT EXISTS tags_taxonomy (
    tag TEXT PRIMARY KEY,
    parent_tag TEXT,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Memory access audit (used by /audit GET + /audit/log POST; previously missing entirely)
CREATE TABLE IF NOT EXISTS memory_access_audit (
    id BIGSERIAL PRIMARY KEY,
    memory_id TEXT,           -- nullable: connector's log_audit() can fire for system-level events with no chunk id
    action TEXT NOT NULL,
    resource_type TEXT DEFAULT 'chunk',
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON memory_access_audit(created_at DESC);

-- Forgetting log
CREATE TABLE IF NOT EXISTS forgetting_log (
    id BIGSERIAL PRIMARY KEY,
    memory_id TEXT,
    action TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- V6.5.0 Phase 1 direction #B (2026-08-23): community detection cache.
-- One row per (community, member). `algorithm` distinguishes Leiden vs
-- Louvain vs label-propagation so re-runs with different algorithms
-- don't trample each other. `partition_at` is wall-clock at the time of
-- the run; downstream code considers rows older than the TTL stale.
CREATE TABLE IF NOT EXISTS memory_graph_communities (
    community_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'leiden',
    partition_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (algorithm, community_id, member_id)
);
CREATE INDEX IF NOT EXISTS memory_graph_communities_member_idx
    ON memory_graph_communities(member_id, algorithm);

-- V6.5.0 Phase 1 direction #B (2026-08-23): PPR result cache. PPR is the
-- expensive step in `_expand_graph_neighbors_ppr` (O(|E| * iter)) so we
-- cache (anchor_id, node_id) -> score with a `computed_at` timestamp and
-- a 1h TTL window. Cache hits short-circuit PPR entirely. The
-- (anchor_id, node_id) PK lets us delete-and-reinsert per anchor
-- without touching unrelated rows.
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

-- V6.5.0 Phase 2 maintenance queue (consolidated from
-- server/src/migrations/2026_09_01_memory_maintenance_queue.sql so new
-- containers pick it up via entrypoint.sh auto-apply).
-- Write-time enqueue + background worker, ADR-075-aligned (no time-driven cron).
CREATE TABLE IF NOT EXISTS memory_maintenance_queue (
    id              BIGSERIAL PRIMARY KEY,
    chunk_id        TEXT NOT NULL REFERENCES memory_chunks(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    retry_count     INTEGER NOT NULL DEFAULT 0,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_at       TIMESTAMPTZ,
    lease_until     TIMESTAMPTZ,
    worker_id       TEXT,
    last_error      TEXT,
    completed_at    TIMESTAMPTZ,
    CONSTRAINT memory_maintenance_queue_chunk_id_unique UNIQUE (chunk_id)
);

-- Worker 拉取索引: (status, enqueued_at) 让 pending 中最早的先出
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_pending_idx
    ON memory_maintenance_queue (enqueued_at)
    WHERE status = 'pending';

-- 监控 / 死信查询索引
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_status_idx
    ON memory_maintenance_queue (status, enqueued_at DESC);

-- Lease 过期回收索引
CREATE INDEX IF NOT EXISTS memory_maintenance_queue_lease_idx
    ON memory_maintenance_queue (lease_until)
    WHERE status = 'processing';

-- V6.6.0 (2026-10-30): RAPTOR summary-tree index.
--
-- Measured on the LOCOMO corpus (8695 chunks, 250 queries): the vector
-- channel put the answer in the top-5 for 58.4% of queries, and for the
-- 46 it missed, the chunks outranking the answer belonged to the SAME
-- conversation in every case — the answer needed many turns read
-- together ("What is Caroline's relationship status?"), so no single turn
-- was a good vector match.
--
-- The tree fixes that: its path score rewards a chunk whose whole cluster
-- is relevant over one that merely looks locally close. Measured: RAPTOR
-- top-5 = 62.8%, union with the vector channel = 65.2%, with 16 answers
-- the vector channel missed entirely.
--
-- The tree is built by a background job (/raptor/rebuild) and persisted
-- here; the search path only walks it in memory.
CREATE TABLE IF NOT EXISTS memory_raptor_trees (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    max_depth INTEGER DEFAULT 2,
    cluster_threshold REAL DEFAULT 0.7,
    node_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    -- The model that encoded the node embeddings. Nodes are only
    -- comparable within one space, so the search path filters by this
    -- exactly as it filters memory_chunks.model_version.
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
    -- The original chunk ids covered by this node: leaves carry exactly
    -- one, internal nodes carry the union of their subtree's chunks.
    chunk_ids TEXT[] DEFAULT '{}'::text[],
    PRIMARY KEY (tree_id, node_id),
    FOREIGN KEY (tree_id) REFERENCES memory_raptor_trees(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS memory_raptor_nodes_tree_idx
    ON memory_raptor_nodes(tree_id);
CREATE INDEX IF NOT EXISTS memory_raptor_nodes_chunk_idx
    ON memory_raptor_nodes(chunk_ids) WHERE is_leaf = TRUE;
CREATE INDEX IF NOT EXISTS memory_raptor_trees_built_idx
    ON memory_raptor_trees(built_at DESC);
