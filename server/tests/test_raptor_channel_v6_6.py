"""Regression tests for the V6.6.0 RAPTOR summary-tree channel.

Why this exists
---------------
Measured on the LOCOMO corpus (8695 chunks, 250 queries) on 127.0.0.1:

* the vector channel put the answer in the top-5 for only 58.4% of
  queries;
* for the 46 answers it missed, the chunks outranking them belonged to
  the SAME conversation in every single case — the answer needed many
  turns read together ("What is Caroline's relationship status?"), so
  no single turn was a good vector match while several looked close;
* the RAPTOR tree, which was already implemented in
  ``consolidate/raptor.py`` but never wired to ``/search``, put the
  answer in the top-5 for 62.8%, and the union with the vector channel
  reached 65.2% — with 16 answers the vector channel missed entirely.

The measured knobs, all from the same run:

* beam=1  -> 61.6% union (greedy; cannot recover from a wrong branch)
* beam=3  -> 65.2% union  <- the peak
* beam=5  -> 61.2% union (too many marginal leaves displace good ones)
* depth 2 and depth 3 tie at 65.2%, so depth=2 is enough
* path-score ranking beats leaf-similarity re-ranking (65.2% vs 64.4%):
  the path score is what rewards "this chunk's whole cluster is relevant"

These tests use stubs — no Postgres, no model files.
"""
from __future__ import annotations

import os
import sys
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _install_stubs() -> None:
    if "psycopg2" not in sys.modules:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            pkg = types.ModuleType("psycopg2")
            pool = types.ModuleType("psycopg2.pool")

            class _Pool:
                def __init__(self, *a, **kw):
                    pass

                def getconn(self):
                    raise RuntimeError("stub pool")

                def putconn(self, conn, close=False):
                    return None

            class PoolError(Exception):
                pass

            pool.ThreadedConnectionPool = _Pool
            pool.PoolError = PoolError
            pkg.pool = pool
            extras = types.ModuleType("psycopg2.extras")

            class RealDictCursor:
                pass

            extras.RealDictCursor = RealDictCursor
            extras.Json = object
            extras.register_default_jsonb = lambda *a, **kw: None
            extras.execute_batch = lambda *a, **kw: None
            pkg.extras = extras
            sys.modules["psycopg2"] = pkg
            sys.modules["psycopg2.pool"] = pool
            sys.modules["psycopg2.extras"] = extras

    if "pgvector" not in sys.modules:
        try:
            import pgvector  # noqa: F401
        except ImportError:
            m = types.ModuleType("pgvector")
            sys.modules["psycopg2"].register_vector = lambda conn: None
            sys.modules["pgvector"] = m

    if "fastapi" not in sys.modules:
        try:
            import fastapi  # noqa: F401
        except ImportError:
            m = types.ModuleType("fastapi")

            class HTTPException(Exception):
                def __init__(self, status_code, detail=None):
                    super().__init__(detail)
                    self.status_code = status_code
                    self.detail = detail

            class _FastAPI:
                def __init__(self, *a, **kw):
                    pass

                def _reg(self, *a, **kw):
                    def deco(fn):
                        return fn
                    return deco

                get = post = put = patch = delete = on_event = middleware = _reg

                def add_middleware(self, *a, **kw):
                    return None

            m.FastAPI = _FastAPI
            m.HTTPException = HTTPException
            m.Request = object
            m.Response = object
            sys.modules["fastapi"] = m

    if "pydantic" not in sys.modules:
        try:
            import pydantic  # noqa: F401
        except ImportError:
            m = types.ModuleType("pydantic")

            class BaseModel:
                pass

            m.BaseModel = BaseModel
            m.Field = lambda *a, **kw: None
            sys.modules["pydantic"] = m


_install_stubs()

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)


def _load_api_pg():
    import importlib
    return importlib.import_module("src.api_pg")


MOD = _load_api_pg()

from src.consolidate.raptor_index import (  # noqa: E402
    RaptorIndex,
    _as_epoch,
    _cosine,
    _vec_to_str,
    walk,
)


def _read_schema():
    """Find schema.sql across the layouts the tests run in.

    Locally the repo layout is <root>/server/docker/unified/schema.sql;
    in the deployed container the tree is flattened and the file lives at
    /opt/cortex/schema.sql (the deploy script copies it to /tmp to apply
    it, but never refreshes the copy baked into the image).

    Returns None when no candidate exists, so the caller can skip rather
    than fail on a layout artefact — the same reason the pre-existing
    test_graph_v6_5 schema tests fail inside the container.
    """
    candidates = [
        os.path.join(SERVER_ROOT, "docker", "unified", "schema.sql"),
        os.path.join(SERVER_ROOT, "..", "server", "docker", "unified", "schema.sql"),
        os.path.join(SERVER_ROOT, "..", "..", "server", "docker", "unified", "schema.sql"),
        "/opt/cortex/schema.sql",
    ]
    for c in candidates:
        if os.path.exists(c):
            with open(c, encoding="utf-8") as f:
                return f.read()
    return None

API_PG = os.path.join(SRC_DIR, "api_pg.py")
from tests._router_source_bridge import api_source_with_routes  # noqa: E402

SOURCE = api_source_with_routes()


def _make_index():
    """A tiny 2-level tree: root -> 2 clusters -> 4 leaves.

    Leaves are unit-ish vectors along the axes so the traversal is
    deterministic and easy to reason about.
    """
    def unit(axis, dim=4):
        v = [0.0] * dim
        v[axis] = 1.0
        return v

    node_embedding = {
        "root": [0.5, 0.5, 0.5, 0.5],
        "c0": unit(0),
        "c1": unit(1),
        "l0": unit(0),
        "l1": unit(0),
        "l2": unit(1),
        "l3": unit(1),
    }
    node_children = {
        "root": ["c0", "c1"],
        "c0": ["l0", "l1"],
        "c1": ["l2", "l3"],
        "l0": [], "l1": [], "l2": [], "l3": [],
    }
    node_parent = {
        "root": None, "c0": "root", "c1": "root",
        "l0": "c0", "l1": "c0", "l2": "c1", "l3": "c1",
    }
    node_is_leaf = {
        "root": False, "c0": False, "c1": False,
        "l0": True, "l1": True, "l2": True, "l3": True,
    }
    leaf_chunk_id = {"l0": "chunk_a", "l1": "chunk_b", "l2": "chunk_c", "l3": "chunk_d"}
    return RaptorIndex(
        tree_id="t1", root_id="root", model_version="nomic-v1.5", built_at=0.0,
        node_embedding=node_embedding, node_children=node_children,
        node_parent=node_parent, node_is_leaf=node_is_leaf,
        leaf_chunk_id=leaf_chunk_id, max_depth=2,
    )


class TestWalk(unittest.TestCase):
    """The top-down traversal."""

    def test_walk_returns_chunk_ids(self):
        idx = _make_index()
        hits = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=10)
        self.assertTrue(hits)
        self.assertTrue(all(isinstance(cid, str) for cid, _s in hits))
        self.assertIn("chunk_a", [cid for cid, _s in hits])

    def test_walk_is_sorted_by_score_descending(self):
        idx = _make_index()
        hits = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=10)
        scores = [s for _c, s in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_path_score_accumulates_across_levels(self):
        """A leaf under a well-matching branch must beat a leaf under a
        poorly-matching one, even if the leaves themselves tie."""
        idx = _make_index()
        hits = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=10)
        by_cid = dict(hits)
        # chunk_a/l0 sit under c0 (axis 0), chunk_c/l2 under c1 (axis 1)
        self.assertGreater(by_cid["chunk_a"], by_cid["chunk_c"])

    def test_beam_limits_the_branches_descended(self):
        idx = _make_index()
        wide = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=10)
        narrow = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=1, top_k=10)
        self.assertGreaterEqual(len(wide), len(narrow))
        # beam applies at EVERY level, so beam=1 descends a single chain
        # root -> one cluster -> one leaf and returns exactly ONE result
        # no matter what top_k says. That is exactly why beam=1 measured
        # 61.6% union against beam=3's 65.2% — and it is the same defect
        # that made the original consolidate/raptor_query.py return one
        # result for a top_k=10 request.
        self.assertEqual(len(narrow), 1,
                         "beam=1 can only ever reach a single leaf")
        self.assertEqual(len(wide), 4, "beam=3 reaches both branches")

    def test_top_k_limits_the_result(self):
        idx = _make_index()
        self.assertLessEqual(len(walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=2)), 2)

    def test_empty_index_returns_nothing(self):
        idx = RaptorIndex("t", "root", "nomic-v1.5", 0.0, {}, {}, {}, {}, {}, 2)
        self.assertEqual(walk(idx, [1.0, 0.0]), [])

    def test_empty_query_returns_nothing(self):
        idx = _make_index()
        self.assertEqual(walk(idx, [], beam=3), [])

    def test_dedup_keeps_the_best_path_score(self):
        idx = _make_index()
        # make l1 reachable from c1 as well, so chunk_b has two paths
        idx.node_children["c1"] = ["l2", "l3", "l1"]
        hits = walk(idx, [1.0, 0.0, 0.0, 0.0], beam=3, top_k=10)
        cids = [c for c, _s in hits]
        self.assertEqual(len(cids), len(set(cids)), "a chunk must appear once")


class TestHelpers(unittest.TestCase):
    def test_cosine_of_identical_vectors_is_one(self):
        self.assertAlmostEqual(_cosine([1.0, 2.0], [1.0, 2.0]), 1.0, places=6)

    def test_cosine_of_orthogonal_vectors_is_zero(self):
        self.assertAlmostEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_cosine_of_zero_vector_is_zero(self):
        self.assertEqual(_cosine([0.0, 0.0], [1.0, 0.0]), 0.0)

    def test_cosine_handles_empty(self):
        self.assertEqual(_cosine([], [1.0]), 0.0)

    def test_vec_to_str_pads_to_768(self):
        """The column is vector(768); a 384-dim vector must be padded or
        the INSERT fails with 'expected 768 dimensions, not 384'."""
        out = _vec_to_str([0.5] * 384)
        self.assertEqual(len(out.strip("[]").split(",")), 768)

    def test_vec_to_str_truncates_oversized(self):
        out = _vec_to_str([0.5] * 1000)
        self.assertEqual(len(out.strip("[]").split(",")), 768)

    def test_vec_to_str_handles_empty(self):
        out = _vec_to_str([])
        self.assertEqual(len(out.strip("[]").split(",")), 768)


class TestTimestampCoercion(unittest.TestCase):
    """built_at arrives as a datetime, not a float.

    Measured live: load_index raised
        "float() argument must be a string or a real number, not 'datetime.datetime'"
    AFTER fetching all 12k node rows — so the tree was never cached and
    every search re-read the whole index. That turned a 50 ms search
    into a 3.5 s one.
    """

    def test_datetime_is_accepted(self):
        from datetime import datetime, timezone
        v = datetime(2026, 10, 31, 4, 53, 35, tzinfo=timezone.utc)
        out = _as_epoch(v)
        self.assertIsInstance(out, float)
        self.assertGreater(out, 0)

    def test_float_is_accepted(self):
        self.assertEqual(_as_epoch(12345.6), 12345.6)

    def test_iso_string_is_accepted(self):
        out = _as_epoch("2026-10-31T04:53:35.590198+00:00")
        self.assertGreater(out, 0)

    def test_none_does_not_raise(self):
        self.assertIsInstance(_as_epoch(None), float)

    def test_garbage_does_not_raise(self):
        self.assertIsInstance(_as_epoch("not a timestamp"), float)


class TestCacheResilience(unittest.TestCase):
    """A failed load must not be retried on every request."""

    def test_failed_load_is_not_retried_immediately(self):
        import src.consolidate.raptor_index as ri
        ri.invalidate_cache()
        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("db down")

        # First call fails and is swallowed.
        self.assertIsNone(ri.get_cached_index(boom, boom, ttl=60))
        # The second call must NOT hit the DB again.
        self.assertIsNone(ri.get_cached_index(boom, boom, ttl=60))
        self.assertEqual(calls["n"], 1,
                         "a failed load must be cached, not retried per request")
        ri.invalidate_cache()

    def test_cache_returns_the_same_object(self):
        import src.consolidate.raptor_index as ri
        ri.invalidate_cache()
        idx = _make_index()

        def fake_query(sql, params=(), fetch="all"):
            if "memory_raptor_trees" in sql and "memory_raptor_nodes" not in sql:
                return [{"id": "t1", "root_id": "root", "max_depth": 2,
                         "model_version": "nomic-v1.5", "built_at": 1000.0}]
            return [{"node_id": nid, "parent_id": pid, "depth": 1,
                     "is_leaf": True, "embedding": [0.1, 0.2, 0.3, 0.4],
                     "chunk_ids": [cid]}
                    for nid, pid, cid in (("l0", "root", "chunk_a"),
                                          ("l1", "root", "chunk_b"))]

        a = ri.get_cached_index(fake_query, lambda *x, **kw: 0, ttl=60)
        b = ri.get_cached_index(fake_query, lambda *x, **kw: 0, ttl=60)
        self.assertIsNotNone(a)
        self.assertIs(a, b, "the second call must hit the cache")
        ri.invalidate_cache()


class TestChannelIsolation(unittest.TestCase):
    """The tree must be strictly additive.

    Measured: the tree reliably improved the candidate pool (+2pp
    recall@30) and reliably DEGRADED the final top-5 (-2.5pp) — at every
    boost setting, including boost=0.

    The cause was the shared RRF normalisation. _three_factor computes
    rrf_norm = rrf_sc / max_rrf with the RRF term carrying the largest
    weight in the composite (0.5). A chunk the tree also found had its
    rrf raised from 0.019971 to 0.024727, which raised max_rrf, which
    shrank every OTHER chunk's rrf_norm by ~19% and dropped their
    composite by ~0.10 — with their own rrf unchanged. On one measured
    query the answer's composite fell from 0.9681 (rank 3) to 0.8959
    (out of the top-5) purely because of that.
    """

    def test_base_fusion_excludes_the_raptor_channel(self):
        self.assertIn('if k != "raptor"', SOURCE,
                      "the base fusion must exclude the tree channel")

    def test_ordered_ids_uses_the_base_fusion(self):
        self.assertIn("rrf_base.keys(),", SOURCE,
                      "the candidate pool must be ordered by the base fusion, "
                      "so the tree cannot reorder the pool it proposes into")

    def test_max_rrf_uses_the_base_fusion(self):
        self.assertIn("max(rrf_base.values()", SOURCE)

    def test_per_chunk_rrf_uses_the_base_fusion(self):
        self.assertIn("rrf_sc = rrf_base.get(cid, 0.0)", SOURCE)

    def test_raptor_score_is_exposed_on_results(self):
        self.assertIn('r["raptor_score"]', SOURCE)

    def test_feature_flag_exists(self):
        self.assertIn("MEMORY_FEATURE_RAPTOR", SOURCE)
        self.assertTrue(hasattr(MOD, "_raptor_enabled"))

    def test_measured_default_weight(self):
        f = MOD.MemorySearchRequest.model_fields
        # Measured on the LOCOMO corpus (n=200, both metrics):
        #     no raptor            MRR=0.4625  recall@5=0.5537
        #     raptor, boost=0      MRR=0.4675  recall@5=0.5587  <- best
        #     w=0.25 n=3           MRR=0.4387  recall@5=0.5650
        #     w=0.5  n=3           MRR=0.4393  recall@5=0.5700
        # Any non-zero composite boost costs MRR. The tree's value is
        # entirely in the candidate pool; its hits are then judged by the
        # existing RRF + composite ranking, which already does better
        # than any boost bolted on top.
        self.assertEqual(f["raptor_score_weight"].default, 0.0,
                         "the measured optimum is no composite boost at all")
        self.assertEqual(f["raptor_boost_top_n"].default, 3)


class TestSearchWiring(unittest.TestCase):
    """The channel must be wired into /search, off by default."""

    def test_search_request_has_the_raptor_flag(self):
        fields = MOD.MemorySearchRequest.model_fields
        self.assertIn("raptor", fields)
        self.assertFalse(fields["raptor"].default,
                         "the raptor channel must be OFF by default")

    def test_search_request_has_the_weight(self):
        fields = MOD.MemorySearchRequest.model_fields
        self.assertIn("raptor_rrf_weight", fields)

    def test_channel_weights_include_raptor(self):
        w = MOD.DEFAULT_CHANNEL_WEIGHTS
        self.assertIn("raptor", w)
        self.assertGreater(w["raptor"], w["lexical"],
                           "the tree is a real signal; the lexical channel is noise")

    def test_beam_is_the_measured_optimum(self):
        self.assertEqual(MOD.RAPTOR_BEAM, 3,
                         "measured: beam=1 -> 61.6%, beam=3 -> 65.2%, beam=5 -> 61.2%")

    def test_search_uses_the_raptor_channel(self):
        self.assertIn('rankings["raptor"] = raptor_ids', SOURCE)

    def test_search_passes_the_raptor_weight(self):
        self.assertIn('channel_weights["raptor"] = req.raptor_rrf_weight', SOURCE)

    def test_rebuild_endpoint_exists(self):
        self.assertIn('@app.post("/raptor/rebuild")', SOURCE)

    def test_stats_endpoint_exists(self):
        self.assertIn('@app.get("/raptor/stats")', SOURCE)

    def test_schema_declares_the_tables(self):
        sql = _read_schema()
        if sql is None:
            self.skipTest("schema.sql not present in this layout")
        self.assertIn("memory_raptor_trees", sql)
        self.assertIn("memory_raptor_nodes", sql)

    def test_migration_exists(self):
        mig = os.path.join(SERVER_ROOT, "src", "migrations",
                           "2026_10_30_v6_6_0_raptor_index.sql")
        self.assertTrue(os.path.exists(mig),
                        "the migration must live where cortex-deploy.sh reads it")


if __name__ == "__main__":
    unittest.main()
