"""Regression tests for the V6.6.0 relation-maintenance fixes.

Audit findings from 127.0.0.1 on 2026-10-31, all measured rather
than inferred:

1. **The temporal relation query silently ignored its own filter.**
   ``GET /relations/temporal/{source_id}`` accepted ``at_time`` and then
   never used it — the WHERE clause had no time predicate at all, so
   asking "what was true at T" returned every relation regardless of T.
   The endpoint answered with plausible rows and the caller could not
   tell they were unfiltered.

2. **Half the bi-temporal model did not exist.** The relation table
   declares ``valid_from`` / ``valid_to`` / ``episode_id``, but no write
   path ever populated them: 0 of 1544 relations had a ``valid_from``.
   So there was nothing for a time filter to filter on.

3. **The decay endpoint and its MCP tool disagreed about everything.**
   The endpoint took ``decay_rate`` as a QUERY parameter (the fraction
   to SUBTRACT); the MCP tool sent ``{"factor": ...}`` in a JSON BODY —
   a name FastAPI does not declare, in a place it does not read for a
   scalar query param. The requested rate was discarded and every call
   decayed by the default 0.01. The two also meant opposite things
   (``factor=0.95`` keeps 95%, ``decay_rate=0.01`` keeps 99%).

4. **The PPR cache had no caller.** ``graph/cache.py`` has existed since
   v6.5.0 with a complete ``compute_or_load()`` contract against the
   ``memory_graph_ppr`` table, but the search path called
   ``top_k_ppr()`` directly — recomputing the power iteration on every
   request. The cache table held 0 rows and the documented "1h TTL
   window" did nothing.

5. **Community detection was dead code.** ``graph/community.py``
   (Leiden / Louvain / label-propagation) and the
   ``memory_graph_communities`` table existed, but nothing called the
   module and the table held 0 rows.

These tests use stubs — no Postgres, no graph libraries.
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

API_PG = os.path.join(SRC_DIR, "api_pg.py")
from tests._router_source_bridge import api_source_with_routes  # noqa: E402

SOURCE = api_source_with_routes()

MCP = os.path.join(SRC_DIR, "mcp_server", "cortex_mcp_server.py")
with open(MCP, encoding="utf-8") as f:
    MCP_SOURCE = f.read()


class TestTemporalFilter(unittest.TestCase):
    """at_time must actually filter."""

    def test_at_time_is_used_in_the_query(self):
        seg = SOURCE[SOURCE.index("def get_temporal_relations"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("at_time", seg)
        self.assertIn("valid_from IS NULL OR valid_from <= %s", seg,
                      "the time predicate must reach the WHERE clause")

    def test_valid_to_is_honoured(self):
        seg = SOURCE[SOURCE.index("def get_temporal_relations"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("valid_to IS NULL OR valid_to > %s", seg)

    def test_response_says_whether_it_filtered(self):
        seg = SOURCE[SOURCE.index("def get_temporal_relations"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn('"filtered_by_time"', seg,
                      "the caller must be able to tell the rows were filtered")

    def test_both_time_columns_are_written(self):
        seg = SOURCE[SOURCE.index("def create_temporal_relation"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("valid_from", seg)
        self.assertIn("valid_to", seg)
        self.assertIn("valid_from, valid_to", seg,
                      "the INSERT must carry the validity window")


class TestDecayContract(unittest.TestCase):
    """The endpoint and the MCP tool must mean the same thing."""

    def test_endpoint_accepts_both_spellings(self):
        seg = SOURCE[SOURCE.index("def link_decay"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("decay_rate", seg)
        self.assertIn("factor", seg)

    def test_supplying_both_is_rejected(self):
        seg = SOURCE[SOURCE.index("def link_decay"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("not both", seg)

    def test_mcp_tool_sends_a_query_param(self):
        seg = MCP_SOURCE[MCP_SOURCE.index("def _tool_links_decay"):]
        seg = seg[: seg.index("\ndef ", 1)]
        self.assertIn('params = {"factor"', seg,
                      "the factor must be built as a query-parameter dict")
        self.assertIn("params=params", seg,
                      "and passed as query parameters, not as a body")
        self.assertNotIn('body={"factor"', seg)
        self.assertNotIn('body=params', seg)

    def test_decay_has_a_dry_run(self):
        """The endpoint is global and destructive; a dry run must exist.

        Learned the hard way: while verifying the fix, calling it twice
        on production halved every link's strength (0.25x) with no way
        to preview the effect. The graph expansion path filters on
        strength, so a repeated decay can quietly disable the graph.
        """
        seg = SOURCE[SOURCE.index("def link_decay"):]
        seg = seg[: seg.index(chr(10) + "@app.", 1)]
        self.assertIn("dry_run", seg)
        self.assertIn("SELECT count(*)", seg,
                      "the dry run must count what it would change")


class TestPprCacheWiring(unittest.TestCase):
    """The PPR cache must be used, not just exist."""

    def test_search_path_uses_compute_or_load(self):
        seg = SOURCE[SOURCE.index("def _expand_graph_neighbors_ppr"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("compute_or_load", seg,
                      "the expansion must go through the cached path")
        # The only legitimate mention of top_k_ppr is in a comment or a
        # log message; an actual CALL would look like an assignment.
        self.assertNotIn("= top_k_ppr(", seg,
                         "top_k_ppr recomputes on every call and never caches")
        self.assertNotIn("ranked = top_k_ppr", seg)

    def test_the_cache_module_is_importable(self):
        import importlib
        mod = importlib.import_module("graph.cache")
        self.assertTrue(hasattr(mod, "compute_or_load"))


class TestCommunityEndpoint(unittest.TestCase):
    """Community detection must have a consumer."""

    def test_detection_endpoint_exists(self):
        self.assertIn('@app.post("/graph/communities")', SOURCE)

    def test_listing_endpoint_exists(self):
        self.assertIn('@app.get("/graph/communities")', SOURCE)

    def test_detection_persists_to_the_table(self):
        seg = SOURCE[SOURCE.index("def graph_detect_communities"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("memory_graph_communities", seg)

    def test_detection_uses_the_existing_module(self):
        seg = SOURCE[SOURCE.index("def graph_detect_communities"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("detect_communities", seg)

    def test_community_attribute_names_are_correct(self):
        """Community has .id and .member_ids, not .community_id/.members."""
        seg = SOURCE[SOURCE.index("def graph_detect_communities"):]
        seg = seg[: seg.index("\n@app.", 1)]
        self.assertIn("comm.id", seg)
        self.assertIn("comm.member_ids", seg)
        self.assertNotIn("comm.community_id", seg)
        self.assertNotIn("comm.members", seg)


class TestLifecycleFilters(unittest.TestCase):
    """Deleted and superseded memories must not be served.

    Measured on 127.0.0.1: the search path filtered ``archived = 0``
    but NOT ``is_deprecated``. Those are different things — archived
    means "deleted", deprecated means "replaced by a newer fact" (the
    conflict resolver sets is_deprecated / superseded_by, and the
    governance stats count the two as separate buckets). Serving a
    superseded fact contradicts the service's own model.

    The graph had the same gap, plus dangling edges: a deleted chunk
    keeps its relations, so the graph still pointed at it. Those
    neighbours consumed the expansion budget (the cap is 10) while
    _fetch_chunks dropped them at the end — the slot was wasted and
    the live neighbour that would have replaced it never got a chance.
    """

    def test_search_excludes_deprecated(self):
        self.assertIn('"is_deprecated = FALSE"', SOURCE,
                      "a superseded fact must not be served as current")

    def test_graph_neighbour_queries_filter_the_lifecycle(self):
        seg = SOURCE[SOURCE.index("def _expand_graph_neighbors"):]
        seg = seg[: seg.index("def _expand_graph_neighbors_filtered")]
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("c1.archived = 0", seg)

    def test_graph_index_filters_the_lifecycle(self):
        idx_path = os.path.join(SRC_DIR, "graph", "index.py")
        with open(idx_path, encoding="utf-8") as f:
            gidx = f.read()
        # Four graph queries (scoped + full, relations + links), each with
        # one clause per joined chunk -> eight clauses in total.
        self.assertEqual(gidx.count("is_deprecated = FALSE"), 8,
                         "all four graph queries must filter both endpoints")
        # relations queries join c1/c2, links queries join cs/ct.
        self.assertEqual(gidx.count("c1.archived = 0"), 2)
        self.assertEqual(gidx.count("c2.archived = 0"), 2)
        self.assertEqual(gidx.count("cs.archived = 0"), 2)
        self.assertEqual(gidx.count("ct.archived = 0"), 2)
        self.assertEqual(gidx.count("JOIN memory_chunks"), 8,
                         "every graph query must join the chunk table")


class TestRelationPrune(unittest.TestCase):
    """Orphaned edges must be prunable.

    Measured on 127.0.0.1 (2132 links): 931 had both endpoints
    alive, 90 had one archived, 451 had one endpoint no longer
    existing, and 594 had BOTH endpoints gone. So 56% of the link
    graph was dangling. The relation tables have no foreign keys, so
    deleting a chunk leaves its edges behind and nothing cleaned them
    up — the dead edges then consumed the neighbour budget.
    """

    def test_prune_endpoint_exists(self):
        self.assertIn('@app.post("/relations/prune")', SOURCE)

    def test_dry_run_is_the_default(self):
        seg = SOURCE[SOURCE.index("def relations_prune"):]
        seg = seg[: seg.index(chr(10) + "@app.", 1)]
        self.assertIn('payload.get("dry_run", True)', seg,
                      "a deleting endpoint must preview by default")

    def test_delete_matches_the_dry_run_definition(self):
        """The dry run and the delete must count the same rows."""
        seg = SOURCE[SOURCE.index("def relations_prune"):]
        seg = seg[: seg.index(chr(10) + "@app.", 1)]
        self.assertIn("dead_filter", seg)
        # definition + the two count queries + the two DELETE statements
        self.assertGreaterEqual(seg.count("dead_filter"), 5,
                                "the same predicate must drive both the "
                                "counts and the deletes, or the dry run "
                                "over-reports")
        # both DELETEs must use it
        self.assertEqual(seg.count("DELETE FROM memory_relations"), 1)
        self.assertEqual(seg.count("DELETE FROM memory_links"), 1)
        deletes = seg[seg.index("removed_rel = execute"""):]
        self.assertGreaterEqual(deletes.count("dead_filter"), 2)


class TestBulkInsertConstraints(unittest.TestCase):
    """The batched relation insert must survive Postgres' two limits.

    Found the hard way: after batching, mining 11224 anchors produced
    33383 pairs and inserted ZERO, with the reason buried in a
    swallowed warning:

        ON CONFLICT DO UPDATE command cannot affect row a second time

    The mining links each anchor to its nearest neighbours, so (A,B) is
    emitted once as A->B and again as B->A, and both sort to the same
    (a,b) key. Postgres aborts the whole statement on a duplicate.
    Separately, 33383 pairs x 5 bind parameters is 166915, and
    Postgres allows at most 65535.
    """

    def test_bulk_insert_dedupes_pairs(self):
        seg = SOURCE[SOURCE.index("def _insert_rels_bulk"):]
        seg = seg[: seg.index(chr(10) + chr(10) + "def ", 1)] if (chr(10) + chr(10) + "def ") in seg else seg
        self.assertIn("_dedupe_relation_pairs(pairs)", seg,
                      "the bulk insert must de-duplicate before inserting")

        helper = SOURCE[SOURCE.index("def _dedupe_relation_pairs"):]
        helper = helper[: helper.index(chr(10) + chr(10) + "def ", 1)]             if (chr(10) + chr(10) + "def ") in helper else helper
        self.assertIn("sorted([mid1, mid2])", helper,
                      "the pair key must be the sorted endpoints")

    def test_bulk_insert_chunks_the_statement(self):
        seg = SOURCE[SOURCE.index("def _insert_rels_bulk"):]
        seg = seg[: seg.index(chr(10) + chr(10) + "def ", 1)] if (chr(10) + chr(10) + "def ") in seg else seg
        self.assertIn("chunk = 500", seg,
                      "the statement must be chunked under the 65535 "
                      "bind-parameter limit")
        self.assertIn("for i in range(0, len(rows), chunk)", seg)

    def test_the_dedupe_keeps_the_strongest_claim(self):
        helper = SOURCE[SOURCE.index("def _dedupe_relation_pairs"):]
        helper = helper[: helper.index(chr(10) + chr(10) + "def ", 1)]             if (chr(10) + chr(10) + "def ") in helper else helper
        self.assertIn("strength > prev[3]", helper,
                      "the strength is the FOURTH element of the tuple; "
                      "reading prev[2] compares a float to a string and "
                      "raises TypeError")


class TestRelationPairDedupe(unittest.TestCase):
    """The dedupe itself, exercised directly.

    This is the logic that silently produced zero relations: Postgres
    aborts a whole INSERT when the same conflict target appears twice,
    and the mining emits (A,B) both as A->B and as B->A.
    """

    def test_reversed_pair_collapses_to_one(self):
        d = MOD._dedupe_relation_pairs([
            ("A", "B", "similar", 0.9),
            ("B", "A", "similar", 0.7),
        ])
        self.assertEqual(len(d), 1)
        self.assertIn(("A", "B"), d)

    def test_the_strongest_claim_wins(self):
        d = MOD._dedupe_relation_pairs([
            ("A", "B", "similar", 0.9),
            ("B", "A", "similar", 0.7),
        ])
        self.assertEqual(d[("A", "B")][3], 0.9)

    def test_distinct_pairs_are_kept(self):
        d = MOD._dedupe_relation_pairs([
            ("A", "B", "similar", 0.9),
            ("A", "C", "similar", 0.5),
            ("D", "E", "similar", 0.7),
        ])
        self.assertEqual(len(d), 3)

    def test_the_index_of_the_strength_is_the_last_one(self):
        """A regression: the strength was read from prev[2] (a string),
        which raised TypeError: '>' not supported between 'float' and
        'str' and turned the whole mining run into a 500."""
        d = MOD._dedupe_relation_pairs([
            ("A", "B", "similar", 0.9),
            ("B", "A", "similar", 0.7),
        ])
        self.assertIsInstance(d[("A", "B")][3], float)
        self.assertIsInstance(d[("A", "B")][2], str)


class TestGraphRankingSignal(unittest.TestCase):
    """The graph as a RANKING signal, not a candidate expansion.

    Measured on LOCOMO with a dense graph (85% of chunks related, 26125
    relations), expanding the candidate pool along graph edges made the
    final top-5 slightly WORSE:

        no graph                 MRR=0.4829  recall@5=0.5760
        include_related gate=6   MRR=0.4819  recall@5=0.5700
        include_related gate=0   MRR=0.4827  recall@5=0.5700

    Relaxing the importance gate changed nothing, so the gate was never
    the blocker: edges are mined at cosine >= 0.72, so the vector channel
    already retrieves the neighbours and the expansion only added
    duplicates.

    The graph still carries a signal the vector channel does not: a
    memory connected to SEVERAL of the query's top hits. That belongs in
    the ranking, and it has to be ADDITIVE and ISOLATED -- the same
    trap the RAPTOR channel walked into (a new channel raising max_rrf
    silently rescales every other candidate).
    """

    def _fake_db(self, rels=(), links=()):
        def q(sql, params=()):
            if "memory_relations" in sql:
                return list(rels)
            if "memory_links" in sql:
                return list(links)
            return []
        return q

    def test_signal_belongs_to_the_pool_member(self):
        """A bug worth locking: the score was accumulated onto the
        ANCHOR instead of the pool member that connects to it."""
        rels = [{"memory_id_1": "A", "memory_id_2": "B", "strength": 0.9}]
        out = MOD._graph_signal(["A"], ["A", "B"], db_query=self._fake_db(rels=rels))
        self.assertEqual(out.get("B"), 0.9,
                         "B is connected to the anchor A")
        self.assertNotIn("A", out,
                         "the anchor itself is not scored by its own edge")

    def test_links_count_too(self):
        links = [{"source_id": "A", "target_id": "C", "strength": 0.4}]
        out = MOD._graph_signal(["A"], ["A", "C"], db_query=self._fake_db(links=links))
        self.assertEqual(out.get("C"), 0.4)

    def test_several_anchors_accumulate(self):
        rels = [
            {"memory_id_1": "A", "memory_id_2": "X", "strength": 0.5},
            {"memory_id_1": "B", "memory_id_2": "X", "strength": 0.5},
        ]
        out = MOD._graph_signal(["A", "B"], ["A", "B", "X"],
                                db_query=self._fake_db(rels=rels))
        # two edges to two anchors, divided by the anchor count (2)
        self.assertAlmostEqual(out.get("X"), 0.5, places=6)

    def test_score_is_capped_at_one(self):
        rels = [{"memory_id_1": "A", "memory_id_2": "X", "strength": 5.0}]
        out = MOD._graph_signal(["A"], ["A", "X"], db_query=self._fake_db(rels=rels))
        self.assertLessEqual(out.get("X"), 1.0)

    def test_no_anchors_or_no_pool_returns_empty(self):
        self.assertEqual(MOD._graph_signal([], ["A"]), {})
        self.assertEqual(MOD._graph_signal(["A"], []), {})

    def test_unconnected_members_get_no_score(self):
        rels = [{"memory_id_1": "A", "memory_id_2": "B", "strength": 0.9}]
        out = MOD._graph_signal(["A"], ["A", "B", "Z"], db_query=self._fake_db(rels=rels))
        self.assertNotIn("Z", out)

    def test_a_failing_db_is_ignored_not_fatal(self):
        def boom(sql, params=()):
            raise RuntimeError("db down")
        self.assertEqual(MOD._graph_signal(["A"], ["A", "B"], db_query=boom), {})

    def test_the_weight_defaults_to_off(self):
        f = MOD.MemorySearchRequest.model_fields
        self.assertEqual(f["graph_score_weight"].default, 0.0,
                         "the base ranking must be unchanged until the "
                         "signal has been measured")
        self.assertEqual(f["graph_anchor_count"].default, 5)

    def test_the_signal_is_additive_not_inside_rrf_norm(self):
        """Adding a channel inside rrf_norm would rescale every other
        candidate -- the RAPTOR bug."""
        seg = SOURCE[SOURCE.index("def _three_factor"):]
        seg = seg[: seg.index("return (0.5 * rrf_norm", 200)]
        self.assertIn("graph_term = _graph_weight * graph_score", seg)
        self.assertNotIn("rrf_norm = rrf_sc / (max_rrf + graph", seg,
                         "the graph must not enter the shared normalization")


class TestGraphExpansionGates(unittest.TestCase):
    """The graph expansion must not be silently disabled by a constant.

    Measured on 127.0.0.1: the neighbour gate was a hardcoded
    ``importance >= 6``. Importance is not a uniform property of a
    corpus — it is whatever the writer set — and 42% of the production
    chunks that HAVE relations sit at importance 5 (so they were never
    expandable), while on the LOCOMO corpus (a flat import at
    importance 5) 96% of them were blocked. That is why include_related
    measured ~0 gain: the graph was gated, not merely sparse.

    Likewise the mining anchor cap was 200, covering 1.8% of an
    11224-chunk corpus.
    """

    def test_neighbour_gate_is_request_tunable(self):
        f = MOD.MemorySearchRequest.model_fields
        self.assertIn("graph_neighbor_min_importance", f)
        self.assertEqual(f["graph_neighbor_min_importance"].default, 6,
                         "the tuned value stays the default")

    def test_the_gate_is_not_hardcoded(self):
        seg = SOURCE[SOURCE.index("added_neighbors and cid in added_neighbors"):]
        seg = seg[: seg.index("continue", 40)]
        self.assertIn("req.graph_neighbor_min_importance", seg,
                      "the threshold must come from the request, not a literal")
        self.assertNotIn("< 6)", seg,
                         "the old hardcoded literal must be gone")

    def test_mining_anchor_cap_covers_the_corpus(self):
        seg = SOURCE[SOURCE.index("def _graph_mine_sync"):]
        seg = seg[: seg.index("def ", 20)] if "def " in seg[20:] else seg
        self.assertIn('"similar_batch", 20000', SOURCE,
                      "the anchor cap must not limit the graph to 1.8% of the corpus")


class TestGraphWiring(unittest.TestCase):
    """The graph package must be reachable the way the search path uses it."""

    def test_graph_package_is_on_the_path(self):
        self.assertIn("from graph import GraphIndex", SOURCE)
        self.assertIn("from graph.cache import compute_or_load", SOURCE)


if __name__ == "__main__":
    unittest.main()
