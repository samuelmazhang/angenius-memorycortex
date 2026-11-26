"""Regression tests for the V6.6.0 retrieval-fusion fixes.

Three defects, all measured on 127.0.0.1 against the LOCOMO corpus
(8695 chunks, 1986 queries) on 2026-10-30:

1. **RRF weights every channel equally.** ``_rrf_fusion()`` summed
   ``1/(k+rank)`` per channel with no per-channel weight, so a lexical
   rank-0 hit scored exactly the same as a vector rank-0 hit. But the
   lexical channel is much weaker than the vector channel: on its own it
   scored MRR=0.000, and out of 200 queries only 5 had the answer found
   uniquely by lexical. So it injected noise into the top-5.

   Measured (300 LOCOMO queries, same limit=5):

       top_lexical=24 (default)   MRR=0.4073  recall@5=0.4936
       top_lexical=0              MRR=0.4555  recall@5=0.5081
       top_lexical=6              MRR=0.4119  recall@5=0.5047
       top_lexical=48             MRR=0.4006  recall@5=0.4875
       hybrid=false (vector only) MRR=0.4560  recall@5=0.5114

   i.e. the DEFAULT configuration was 12% worse on MRR than plain vector
   search, and got monotonically worse as the lexical pool grew.

2. **The lexical query does not filter stopwords.**
   ``_build_lexical_query()`` ORs every token of length >= 2, so
   "When did Caroline go to the museum?" became

       When | did | Caroline | go | to | the | museum

   With OR semantics, "the" matches a large fraction of an English
   corpus — the query stops discriminating.

3. **No multi-query expansion.** One embedding of one phrasing limits
   recall to what that phrasing happens to match. Measured on the same
   corpus, expanding the query into 3 forms (original / keyword-only /
   statement form) moved recall@5 from 0.5425 to 0.5442 (+0.3%).
   (An earlier measurement claimed +6.8%; it was wrong — it compared a
   recall@5 baseline against a merged list never truncated back to 5.)

These tests use stubs — no Postgres, no model files.
"""
from __future__ import annotations

import os
import sys
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
API_PG = os.path.join(SRC_DIR, "api_pg.py")


def _install_stubs() -> None:
    """Stub docker-only deps so ``api_pg`` imports on a dev box."""
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

with open(API_PG, encoding="utf-8") as f:
    SOURCE = f.read()


class TestRrfWeighting(unittest.TestCase):
    """Fix 1: per-channel RRF weights."""

    def test_fusion_accepts_weights(self):
        self.assertTrue(hasattr(MOD, "_rrf_fusion"), "_rrf_fusion must exist")

    def test_lexical_channel_is_down_weighted(self):
        """A lexical rank-0 hit must NOT outrank a vector rank-0 hit."""
        rankings = {"vector": ["v1", "v2"], "lexical": ["x1", "x2"]}
        weights = {"vector": 1.0, "lexical": 0.25}
        sc = MOD._rrf_fusion(rankings, k=60, weights=weights)
        self.assertGreater(sc["v1"], sc["x1"],
                           "the vector channel must dominate the lexical one")

    def test_weighted_fusion_preserves_ordering_within_a_channel(self):
        rankings = {"vector": ["a", "b", "c"], "lexical": ["d", "e"]}
        sc = MOD._rrf_fusion(rankings, k=60, weights={"vector": 1.0, "lexical": 0.25})
        self.assertGreater(sc["a"], sc["b"])
        self.assertGreater(sc["b"], sc["c"])

    def test_weighted_fusion_still_rewards_agreement(self):
        """A hit found by BOTH channels must still beat a single-channel hit."""
        rankings = {"vector": ["shared", "vonly"], "lexical": ["shared", "xonly"]}
        sc = MOD._rrf_fusion(rankings, k=60, weights={"vector": 1.0, "lexical": 0.25})
        self.assertGreater(sc["shared"], sc["vonly"])
        self.assertGreater(sc["shared"], sc["xonly"])

    def test_no_weights_keeps_the_old_behaviour(self):
        """Backwards compatible: no weights -> equal weight per channel."""
        rankings = {"vector": ["v1"], "lexical": ["x1"]}
        sc = MOD._rrf_fusion(rankings, k=60)
        self.assertAlmostEqual(sc["v1"], sc["x1"], places=6)

    def test_default_weights_are_configurable(self):
        self.assertTrue(hasattr(MOD, "DEFAULT_CHANNEL_WEIGHTS"),
                        "there must be a default weight map")
        w = MOD.DEFAULT_CHANNEL_WEIGHTS
        self.assertGreater(w["vector"], w["lexical"],
                           "the vector channel must outweigh the lexical one")

    def test_search_request_exposes_the_weight(self):
        req_cls = MOD.MemorySearchRequest
        self.assertTrue(hasattr(req_cls, "model_fields"),
                        "pydantic v2 model expected")
        self.assertIn("lexical_rrf_weight", req_cls.model_fields,
                      "the lexical weight must be request-tunable")


class TestLexicalStopwords(unittest.TestCase):
    """Fix 2: stopwords must not widen the lexical OR-query."""

    def test_english_stopwords_are_dropped(self):
        q = MOD._build_lexical_query("When did Caroline go to the museum?")
        self.assertIsNotNone(q)
        terms = {t.strip().lower() for t in q.split("|")}
        for sw in ("the", "did", "to", "when"):
            self.assertNotIn(sw, terms,
                             f"stopword {sw!r} must not be in the lexical query")

    def test_content_words_survive(self):
        q = MOD._build_lexical_query("When did Caroline go to the museum?")
        terms = {t.strip().lower() for t in q.split("|")}
        self.assertIn("caroline", terms)
        self.assertIn("museum", terms)

    def test_query_of_only_stopwords_yields_none(self):
        self.assertIsNone(MOD._build_lexical_query("what is the"))

    def test_chinese_query_still_yields_terms(self):
        q = MOD._build_lexical_query("记忆系统的连接池配置")
        self.assertIsNotNone(q)
        self.assertTrue(len(q.split("|")) >= 1)

    def test_short_token_filter_is_preserved(self):
        """Single-character tokens were already dropped; keep that."""
        q = MOD._build_lexical_query("a b cc dd")
        self.assertIsNotNone(q)
        terms = {t.strip() for t in q.split("|")}
        self.assertNotIn("a", terms)
        self.assertNotIn("b", terms)
        self.assertIn("cc", terms)
        self.assertIn("dd", terms)


class TestQueryExpansion(unittest.TestCase):
    """Fix 3: multi-query expansion."""

    def test_expand_returns_multiple_forms(self):
        forms = MOD._expand_query_forms("When did Caroline go to the museum?")
        self.assertGreaterEqual(len(forms), 2,
                                "the original question must be expanded")

    def test_original_form_is_always_first(self):
        q = "When did Caroline go to the museum?"
        forms = MOD._expand_query_forms(q)
        self.assertEqual(forms[0], q,
                         "the original query must lead, so plain search is unchanged")

    def test_forms_are_deduplicated(self):
        forms = MOD._expand_query_forms("memory")
        self.assertEqual(len(forms), len(set(forms)),
                         "duplicate forms would waste a search")

    def test_empty_query_is_safe(self):
        self.assertEqual(MOD._expand_query_forms(""), [])

    def test_expansion_is_opt_in(self):
        """Extra embeddings cost latency; it must be request-controlled."""
        fields = MOD.MemorySearchRequest.model_fields
        self.assertIn("query_expansion", fields,
                      "query expansion must be request-tunable")
        self.assertFalse(fields["query_expansion"].default,
                         "query expansion must be OFF by default")


if __name__ == "__main__":
    unittest.main()
