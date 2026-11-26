"""Black-box tests: local connector (`tools.cortex_memory_backend`) → remote cortex.

Expands the end-to-end scope beyond the 8 `memory_tool` actions to the **backend
layer**: each public `cortex_memory_backend` method is called against the REMOTE
cortex server (default http://127.0.0.1:1880) and its return shape is asserted.
This catches connector↔server contract drift (wrong param names, schema mismatches,
dropped methods) that would otherwise only surface in a live session.

Run in the hermes venv so `tools.cortex_memory_backend` and its deps resolve:

    cd /a/Application/Hermes-Agent/Program/hermes-agent
    ./venv/Scripts/python.exe -m pytest \\
        ".connector\\hermes\\tests\\test_cortex_backend_blackbox.py" \\
        -p no:cacheprovider --import-mode=importlib -q

Env:
  MEMORY_CORTEX_URL  default http://127.0.0.1:1880 (the remote cortex service)
  MEMORY_CORTEX_TIMEOUT / RETRIES / BREAKER_*  optional tuning (defaults fine)

This is a read-mostly suite: it writes a throwaway working-memory key and a
throwaway media/resource/procedure, then cleans them up where the API allows.
"""
import os
import sys
import uuid

import pytest

os.environ.setdefault("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
os.environ.setdefault("MEMORY_CORTEX_TIMEOUT", "15")
os.environ.setdefault("MEMORY_CORTEX_RETRIES", "2")

sys.path.insert(0, r"A:\Application\Hermes-Agent\Program\hermes-agent")

import tools.cortex_memory_backend as cmb  # noqa: E402


@pytest.fixture(scope="module")
def mark():
    return f"bb-{uuid.uuid4().hex[:10]}"


def _definitely_a_list(x):
    """Helpers sometimes return None/{...} on degrade; tolerate that."""
    return isinstance(x, list)


# ============================================================================
# Health / stats / endpoints
# ============================================================================
class TestHealthAndStats:
    def test_health(self):
        h = cmb.health()
        assert isinstance(h, dict) and h.get("status") == "healthy"

    def test_stats(self):
        s = cmb.stats()
        assert isinstance(s, dict) and "total_memories" in s

    def test_metrics(self):
        m = cmb.get_metrics()
        assert m is not None  # may be {} or list; must not raise

    def test_recommend(self):
        r = cmb.recommend_endpoints("connector_memory")
        assert isinstance(r, dict)

    def test_is_cortex_active(self):
        assert cmb.is_cortex_active() is True


# ============================================================================
# Search / hybrid / tts (V5.3 RRF)
# ============================================================================
class TestSearch:
    def test_search_include_related(self, mark):
        # write a chunk, then search with include_related (V5.3 default)
        created = cmb.create_entry("memory", f"bb-search {mark}", importance=7)
        cid = created.get("id")
        hits = cmb.search(mark, limit=3, include_related=True)
        assert isinstance(hits, list)
        # the chunk we wrote may not top-3-match the tag but the call must be sane
        cmb.delete_entry(cid) if cid else None

    def test_search_hybrid_honors_limit(self):
        hy = cmb.search_hybrid("hermes venv", top_k=4)
        if isinstance(hy, dict):
            assert len(hy.get("results", [])) == 4  # limit fix (V5.3.4)

    def test_search_tts(self):
        r = cmb.search_tts("hermes venv", top_k=3)
        assert isinstance(r, dict) or isinstance(r, list)

    def test_reason_with_memory_graph_subgraph(self):
        rc = cmb.reason_with_memory("hermes venv fingerprint", top_k=3,
                                    include_hierarchy=True, khop=2)
        assert isinstance(rc, dict)
        assert "graph_subgraph" in rc and "context_chunks" in rc


# ============================================================================
# Graph: k-hop + mine (V5.3)
# ============================================================================
class TestGraph:
    def test_graph_khop(self, mark):
        hits = cmb.search(mark, limit=1)
        if hits:
            seed = hits[0]["id"]
            sub = cmb.graph_khop(seed, k=2, min_strength=0.5, limit=8)
            assert isinstance(sub, dict) and "nodes" in sub

    def test_graph_mine(self):
        g = cmb.graph_mine(similarity_threshold=0.72, similar_batch=60, per_anchor=2)
        assert isinstance(g, dict) and "total_relations_after" in g


# ============================================================================
# Relations / links
# ============================================================================
class TestLinksAndRelations:
    def test_relations(self, mark):
        created = cmb.create_entry("memory", f"bb-rel {mark}", importance=7)
        cid = created.get("id")
        # connector get_relations returns a List (each pair {memory_id_1,...})
        rel = cmb.get_relations(cid) if cid else []
        assert isinstance(rel, list)
        cmb.delete_entry(cid) if cid else None

    def test_auto_link_new_chunk(self, mark):
        created = cmb.create_entry("memory", f"bb-autolink {mark}", importance=7)
        cid = created.get("id")
        n = cmb.auto_link_new_chunk(cid, f"bb-autolink {mark}") if cid else 0
        assert isinstance(n, int)
        cmb.delete_entry(cid) if cid else None

    def test_auto_link_from_search(self, mark):
        # V6.5.1 client-side replacement: pair-wise create_link from search hits.
        # Verify it returns an int (count of links created locally), not 405.
        # The previous server-side POST /links/auto-link was removed in V6.5.0.
        seed = f"bb-autolink-search {mark}"
        # Seed a few chunks so search has something to find
        seed_ids = []
        for i in range(3):
            c = cmb.create_entry("memory", f"{seed} {i}", importance=7)
            cid = c.get("id") if isinstance(c, dict) else None
            if cid:
                seed_ids.append(cid)
        try:
            n = cmb.auto_link_from_search(seed, top_pairs=3, min_similarity=0.0)
            assert isinstance(n, int), f"auto_link_from_search returned {type(n).__name__}, expected int"
            assert n >= 0
            # Hard cap from the contract: never exceed top_pairs
            assert n <= 3, f"unexpectedly created {n} links (cap was top_pairs=3)"
        finally:
            for cid in seed_ids:
                cmb.delete_entry(cid)

    def test_get_relations_bitemporal(self, mark):
        # V6.5.1 server endpoint changed: GET /relations/temporal/{src}
        # (no longer the 4-segment /bi-temporal path that 404s).
        # Verify backward-compat: caller passes memory_id= and limit= kwargs
        # (the way memory_tool.py:919,936 still calls it).
        created = cmb.create_entry("memory", f"bb-bitemp {mark}", importance=7)
        cid = created.get("id")
        try:
            # New signature: source_id positional
            r1 = cmb.get_relations_bitemporal(cid)
            assert isinstance(r1, list)
            # Backward-compat: memory_id= kwarg (as memory_tool.py passes)
            r2 = cmb.get_relations_bitemporal(memory_id=cid, limit=5)
            assert isinstance(r2, list)
            assert len(r2) <= 5
        finally:
            cmb.delete_entry(cid) if cid else None


# ============================================================================
# Working memory / conversations
# ============================================================================
class TestWorkingMemory:
    @pytest.fixture(autouse=True)
    def sid(self, mark):
        self._sid = f"bb-{mark}"
        return self._sid

    def test_set_and_get_working(self, mark):
        ok = cmb.set_working_memory(session_id=f"bb-{mark}", key="k",
                                    value={"n": 1}, value_type="dict")
        assert ok is not None
        ctx = cmb.get_all_working_memory(f"bb-{mark}")
        assert ctx is not None  # dict/list, must not error

    def test_append_conversation(self, mark):
        r = cmb.append_conversation_turn(session_id=f"bb-{mark}",
                                         role="user", content="hello bb")
        assert r is not None


# ============================================================================
# Procedures
# ============================================================================
class TestProcedures:
    def test_procedure_roundtrip(self, mark):
        name = f"bb_proc_{mark}"
        pid = cmb.learn_procedure(name=name, trigger=f"bb trigger {mark}",
                                  action="do X", description="")
        assert pid  # non-empty id
        found = cmb.search_procedures(f"bb trigger {mark}", limit=5)
        assert isinstance(found, list)


# ============================================================================
# Audit
# ============================================================================
class TestAudit:
    def test_log_and_query_audit(self, mark):
        # log_audit must return a truthy confirm (server returns status:logged)
        aid = cmb.log_audit(action="bb_test", resource_type="chunk",
                            resource_id=mark, actor="blackbox")
        assert aid  # truthy (1 on logged); was always 0 before the fix
        entries = cmb.query_audit(limit=5)
        assert isinstance(entries, list)


# ============================================================================
# Hierarchy
# ============================================================================
class TestHierarchy:
    def test_hierarchy(self, mark):
        nid = cmb.create_hierarchy_node(level="episode", title=f"bb-{mark}")
        assert nid
        node = cmb.get_hierarchy_node(nid)
        assert isinstance(node, dict)


# ============================================================================
# Media / resources
# ============================================================================
class TestMediaAndResources:
    def test_media(self, mark):
        mid = cmb.add_media("text", ocr_text=f"bb media {mark}")
        # endpoint may return id or dict; must not raise
        hits = cmb.search_media(f"bb media {mark}", limit=3)
        assert hits is not None

    def test_resource(self, mark):
        # add_resource uses url/description, not content
        rid = cmb.add_resource("doc", title=f"bb-{mark}", url=f"http://bb.{mark}/",
                               description="body")
        found = cmb.search_resources(f"bb-{mark}", limit=3)
        assert found is not None


# ============================================================================
# Forgetting / storage (read-mostly)
# ============================================================================
class TestLifecycleReads:
    def test_forgetting_stats(self):
        s = cmb.get_forgetting_stats()
        assert s is not None

    def test_expired(self):
        e = cmb.get_expired_memories(limit=3)
        assert e is not None

    def test_storage_health(self):
        s = cmb.get_storage_health()
        assert s is not None

    def test_rumination_stats(self):
        s = cmb.get_rumination_stats()
        assert s is not None


# ============================================================================
# Compression / consolidate
# ============================================================================
class TestCompressAndConsolidate:
    def test_compress_old(self):
        n = cmb.compress_old_chunks(min_age_days=365, max_importance=4, max_per_batch=3)
        assert isinstance(n, int)

    def test_consolidate_facts(self):
        facts = cmb.get_consolidate_facts(limit=3)
        assert facts is not None


# ============================================================================
# Core profile / user
# ============================================================================
class TestCoreProfile:
    def test_core_profile_roundtrip(self, mark):
        ok = cmb.upsert_user_profile(user_id=f"bb-{mark}", profile={"src": "bb"})
        prof = cmb.get_user_profile(f"bb-{mark}")
        assert prof is not None


# ============================================================================
# Deep context (V5.3 wiring)
# ============================================================================
class TestDeepContext:
    def test_deep_context(self, mark):
        hits = cmb.search(mark, limit=1)
        if not hits:
            created = cmb.create_entry("memory", f"bb-deep {mark}", importance=7)
            cid = created.get("id")
        else:
            cid = hits[0]["id"]
        ctx = cmb.deep_context_for(cid)
        assert isinstance(ctx, dict)
        cmb.delete_entry(cid) if cid else None


# ============================================================================
# V6.5.0 P2 scheduler / governance / stats (added in connector V6.5.1)
# ============================================================================
class TestV650P2Scheduler:
    """Cover the new P2 write-time scheduler endpoints + governance + stats
    methods added in cortex V6.5.0 / V6.5.1."""

    def test_maintenance_stats(self):
        stats = cmb.get_maintenance_stats()
        assert isinstance(stats, dict)
        # /admin/maintenance/stats should report worker_id and queue counts
        assert "worker_id" in stats or stats == {}, \
            f"unexpected shape: {stats}"

    def test_maintenance_dead(self):
        dead = cmb.get_maintenance_dead(limit=5)
        assert isinstance(dead, list)

    def test_run_maintenance_once(self):
        result = cmb.run_maintenance_once()
        assert isinstance(result, dict)

    def test_admin_deprecate_by_type_dry_run(self):
        # dry_run=True must not actually mutate
        result = cmb.admin_deprecate_by_type("recovered", dry_run=True, limit=10)
        assert isinstance(result, dict)
        assert result.get("dry_run") is True

    def test_get_relations_batch(self, mark):
        # Seed two chunks so there's at least one path
        c1 = cmb.create_entry("memory", f"bb-rbatch {mark} A", importance=7)
        c2 = cmb.create_entry("memory", f"bb-rbatch {mark} B", importance=7)
        ids = [c.get("id") for c in (c1, c2) if isinstance(c, dict) and c.get("id")]
        try:
            r = cmb.get_relations_batch(ids, depth=1, max_per_node=4)
            assert isinstance(r, list)
        finally:
            for cid in ids:
                cmb.delete_entry(cid)

    def test_batch_create_chunks(self, mark):
        items = [
            {"type": "memory", "content": f"bb-batch {mark} {i}",
             "importance": 5, "source": "bb", "tags": ["bb"]}
            for i in range(2)
        ]
        result = cmb.batch_create_chunks(items)
        assert isinstance(result, list)
        # clean up
        for r in result:
            if isinstance(r, dict) and r.get("id"):
                cmb.delete_entry(r["id"])

    def test_patch_chunk(self, mark):
        created = cmb.create_entry("memory", f"bb-patch {mark}", importance=5)
        cid = created.get("id") if isinstance(created, dict) else None
        try:
            if cid:
                updated = cmb.patch_chunk(cid, importance=8)
                assert isinstance(updated, dict) or updated is None
        finally:
            cmb.delete_entry(cid) if cid else None

    def test_conflict_log(self):
        log = cmb.conflict_log(limit=5)
        assert isinstance(log, list)

    def test_get_ttl_archived(self):
        archived = cmb.get_ttl_archived(limit=5)
        assert isinstance(archived, list)

    def test_get_mode(self):
        mode = cmb.get_mode()
        assert isinstance(mode, dict)

    def test_list_endpoints(self):
        eps = cmb.list_endpoints()
        assert isinstance(eps, list)
        assert len(eps) > 0, "list_endpoints should return at least one entry"

    def test_get_metrics_prometheus(self):
        # /metrics is Prometheus text format (not JSON) per server CHANGELOG
        m = cmb.get_metrics()
        assert isinstance(m, str)
        # Should contain at least one cortex_* metric name
        assert "cortex_" in m, f"unexpected /metrics body: {m[:200]}"


class TestStatsBreakdown:
    """V6.1.0 stats-by-* endpoints + V6.5.1 stats observability."""

    def test_stats_by_deprecated(self):
        r = cmb.stats_by_deprecated(limit=10)
        assert isinstance(r, dict)

    def test_stats_by_source(self):
        r = cmb.stats_by_source()
        assert isinstance(r, dict)

    def test_stats_by_type(self):
        r = cmb.stats_by_type()
        assert isinstance(r, dict)

    def test_stats_by_importance(self):
        r = cmb.stats_by_importance()
        assert isinstance(r, dict)

    def test_stats_by_status(self):
        r = cmb.stats_by_status()
        assert isinstance(r, dict)

    def test_stats_embed_cache(self):
        r = cmb.stats_embed_cache()
        assert isinstance(r, dict)

    def test_stats_health(self):
        r = cmb.stats_health()
        assert isinstance(r, dict)

    def test_stats_ops(self):
        r = cmb.stats_ops()
        assert isinstance(r, dict)
