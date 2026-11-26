"""Regression tests for the V6.6.0 embedding-space + connection-pool fixes.

Three defects, all measured live on 127.0.0.1 on 2026-10-30:

1. **Silent embedding-model fallback** (``resolve_embedding_model``)
   The corpus held two incompatible vector spaces under one label::

       true-nomic    2333 chunks   768 real dims
       mpnet-padded   250 chunks   384 real dims + 384 zeros
       both labelled model_version = 'nomic-v1.5'

   The container's baked venv lost ``sentence_transformers``/``torch`` in the
   2026-09-01 image rebuild, so ``get_nomic_model()`` always returned
   "unavailable" and ``embed_text()`` fell back to mpnet *silently* — the
   caller still wrote ``model_version='nomic-v1.5'``. A query embedded in the
   mpnet space can only be compared against the mpnet half: measured,
   30/30 of the vector channel's top-30 came from those 250 chunks; the
   other 2333 (90% of the corpus) were vector-invisible.

2. **Unpadded vectors** (``_vec_to_str_for_model`` vs ``vec_to_str``)
   ``/conflict/upsert`` wrote ``vec_to_str(embed_text(...))`` with no
   padding into a ``vector(768)`` column. With the mpnet fallback that is
   a 384-dim vector -> Postgres raises "expected 768 dimensions, not 384"
   -> the endpoint returned HTTP 500 for **every** write (measured live).

3. **Connection-pool exhaustion**
   ``ThreadedConnectionPool(minconn=2, maxconn=10)`` with 98 sync endpoints.
   Measured: 24 concurrent ``/search`` calls -> 10-13 succeeded, 11-14
   returned HTTP 500 with ``psycopg2.pool.PoolError: connection pool
   exhausted`` in the server log.

These tests do not need Postgres or a live server.
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
# api_pg.py imports its siblings as ``from src.<pkg> import ...``, so the
# directory that *contains* the ``src`` package (i.e. server/) must be
# importable — that is how the container runs it (PYTHONPATH=/app).
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
API_PG = os.path.join(SRC_DIR, "api_pg.py")


def _install_stubs() -> None:
    """Stub the docker-only deps so ``api_pg`` imports on a dev box."""
    if "psycopg2" not in sys.modules:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            pkg = types.ModuleType("psycopg2")
            pool = types.ModuleType("psycopg2.pool")

            class _Pool:
                def __init__(self, *a, **kw):
                    self.args = a
                    self.kwargs = kw

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

            class Json:
                pass

            extras.RealDictCursor = RealDictCursor
            extras.Json = Json
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
            psycopg2 = sys.modules["psycopg2"]
            psycopg2.register_vector = lambda conn: None
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
                    self.routes = []

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
    """Import api_pg as a real package member.

    It uses *relative* imports (``from .rerank import ...``) for some
    siblings and absolute ones (``from src.consolidate...``) for others, so
    it has to be imported as ``src.api_pg`` — not loaded from a file path,
    which would leave the relative imports without a parent package.
    """
    if "src.api_pg" in sys.modules:
        return sys.modules["src.api_pg"]
    import importlib
    return importlib.import_module("src.api_pg")


MOD = _load_api_pg()

with open(API_PG, encoding="utf-8") as f:
    SOURCE = f.read()


class TestResolveEmbeddingModel(unittest.TestCase):
    """The fix for the silent fallback."""

    def test_exists(self):
        self.assertTrue(hasattr(MOD, "resolve_embedding_model"),
                        "resolve_embedding_model() must exist")

    def test_returns_a_supported_model(self):
        for requested in (None, "nomic-v1.5", "mpnet", "bogus"):
            got = MOD.resolve_embedding_model(requested)
            self.assertIn(got, MOD.SUPPORTED_MODELS,
                          f"resolve({requested!r}) returned {got!r}")

    def test_unknown_model_falls_back_to_a_supported_one(self):
        self.assertIn(MOD.resolve_embedding_model("nope"), MOD.SUPPORTED_MODELS)

    def test_nomic_unavailable_falls_back_to_mpnet(self):
        """When nomic cannot load, the *effective* model must be mpnet."""
        original = MOD.get_nomic_model
        try:
            MOD.get_nomic_model = lambda: ("unavailable", None)
            self.assertEqual(MOD.resolve_embedding_model("nomic-v1.5"), "mpnet")
        finally:
            MOD.get_nomic_model = original

    def test_nomic_available_stays_nomic(self):
        original = MOD.get_nomic_model
        try:
            MOD.get_nomic_model = lambda: ("st_model", object())
            self.assertEqual(MOD.resolve_embedding_model("nomic-v1.5"), "nomic-v1.5")
        finally:
            MOD.get_nomic_model = original

    def test_mpnet_request_is_never_remapped(self):
        original = MOD.get_nomic_model
        try:
            MOD.get_nomic_model = lambda: ("unavailable", None)
            self.assertEqual(MOD.resolve_embedding_model("mpnet"), "mpnet")
        finally:
            MOD.get_nomic_model = original


class TestVectorDimensions(unittest.TestCase):
    """The fix for the unpadded-vector 500s."""

    def test_vec_to_storage_str_pads_384_to_768(self):
        v = [0.1] * 384
        s = MOD.vec_to_storage_str(v, target_dim=768)
        self.assertTrue(s.startswith("["))
        self.assertEqual(s.count(",") + 1, 768)

    def test_vec_to_storage_str_keeps_768(self):
        v = [0.1] * 768
        s = MOD.vec_to_storage_str(v, target_dim=768)
        self.assertEqual(s.count(",") + 1, 768)

    def test_vec_to_str_does_not_pad(self):
        """vec_to_str is the raw helper — it must stay unpadded so the
        difference from the safe helper remains visible."""
        s = MOD.vec_to_str([0.1] * 384)
        self.assertEqual(s.count(",") + 1, 384)

    def test_for_model_pads_mpnet_to_768(self):
        s = MOD._vec_to_str_for_model([0.1] * 384, "mpnet")
        self.assertEqual(s.count(",") + 1, 768,
                         "an mpnet vector must be padded to the 768-wide column")


class TestSourceWiring(unittest.TestCase):
    """The write/read paths must use the effective model."""

    def test_write_paths_use_resolve_embedding_model(self):
        # create_chunk, batch, update_chunk, conflict/upsert, and both
        # search paths must all resolve the effective model before encoding.
        for needle in (
            "actual_model = resolve_embedding_model(selected_model)",
            "actual_item_model = resolve_embedding_model(item_model)",
            "actual_new_model = resolve_embedding_model(new_model)",
            "actual_selected_model = resolve_embedding_model(selected_model)",
            "upsert_model = resolve_embedding_model(EMBEDDING_MODEL_ENV)",
            "query_model = resolve_embedding_model(query_model)",
            "qm = resolve_embedding_model(qm)",
        ):
            self.assertIn(needle, SOURCE,
                          f"missing wiring: {needle}")

    def test_conflict_upsert_no_longer_uses_unpadded_vec_to_str(self):
        self.assertNotIn(
            "emb_str = vec_to_str(embed_text(content))", SOURCE,
            "/conflict/upsert must pad its vector to the storage dimension",
        )

    def test_conflict_upsert_records_model_version(self):
        m = re.search(
            r"INSERT INTO memory_chunks\s*\n\s*\(id, content, type, tags, source, "
            r"importance, metadata,\s*\n\s*embedding, tier, archived, model_version",
            SOURCE,
        )
        self.assertIsNotNone(
            m,
            "/conflict/upsert INSERT must include the model_version column",
        )

    def test_graph_fallback_pads_and_filters_by_model(self):
        seg = SOURCE[SOURCE.index("def _expand_graph_neighbors_filtered"):]
        seg = seg[: seg.index("def _graph_ppr_enabled")]
        self.assertIn("_vec_to_str_for_model(query_emb", seg)
        self.assertIn("model_version IS NULL OR model_version = %s", seg)


class TestConnectionPool(unittest.TestCase):
    """The fix for the pool-exhaustion 500s."""

    def test_pool_size_is_configurable(self):
        # v6.6 P0 phase 2: pool sizing moved behind the CortexConfig singleton.
        # The contract is "configurable via env through the single config source",
        # not a specific getenv spelling in api_pg.py.
        with open(os.path.join(SRC_DIR, "config.py"), encoding="utf-8") as f:
            cfg_source = f.read()
        self.assertIn("os.getenv(\"PG_POOL_MAX\"", cfg_source)
        self.assertIn("os.getenv(\"PG_POOL_MIN\"", cfg_source)
        self.assertIn("_PG_POOL_MAX = _cfg.pg_pool_max", SOURCE)
        self.assertIn("_PG_POOL_MIN = _cfg.pg_pool_min", SOURCE)

    def test_pool_is_no_longer_hardcoded_to_ten(self):
        self.assertNotIn("minconn=2, maxconn=10,", SOURCE,
                         "the hard-coded maxconn=10 must be gone")

    def test_get_conn_waits_instead_of_failing_fast(self):
        self.assertIn("psycopg2.pool.PoolError", SOURCE)
        self.assertIn("_PG_POOL_WAIT_SECONDS", SOURCE)

    def test_get_conn_retries_then_raises(self):
        """A busy pool must degrade to latency, not to an immediate error."""
        import psycopg2.pool as poolmod
        import time as _t

        original = MOD.pg_pool.getconn
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise poolmod.PoolError("connection pool exhausted")
            return "conn"

        MOD.pg_pool.getconn = flaky
        old_wait = MOD._PG_POOL_WAIT_SECONDS
        MOD._PG_POOL_WAIT_SECONDS = 5.0
        try:
            got = MOD.get_conn()
            self.assertEqual(got, "conn")
            self.assertGreaterEqual(calls["n"], 3,
                                    "get_conn must retry before succeeding")
        finally:
            MOD.pg_pool.getconn = original
            MOD._PG_POOL_WAIT_SECONDS = old_wait

    def test_get_conn_raises_after_the_wait_window(self):
        import psycopg2.pool as poolmod

        original = MOD.pg_pool.getconn

        def always_busy():
            raise poolmod.PoolError("connection pool exhausted")

        MOD.pg_pool.getconn = always_busy
        old_wait = MOD._PG_POOL_WAIT_SECONDS
        MOD._PG_POOL_WAIT_SECONDS = 0.3
        try:
            with self.assertRaises(poolmod.PoolError):
                MOD.get_conn()
        finally:
            MOD.pg_pool.getconn = original
            MOD._PG_POOL_WAIT_SECONDS = old_wait


class TestRepairEtlExists(unittest.TestCase):
    """The repair path for the already-corrupted rows."""

    def test_repair_script_present(self):
        p = os.path.join(SRC_DIR, "etl", "etl_repair_mislabeled.py")
        self.assertTrue(os.path.exists(p), "repair ETL must exist")

    def test_repair_script_detects_by_vector_shape(self):
        """It must NOT select by model_version alone — that is what is wrong."""
        p = os.path.join(SRC_DIR, "etl", "etl_repair_mislabeled.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("NONZERO_DIM_THRESHOLD", src)
        self.assertIn("string_to_array", src)


if __name__ == "__main__":
    unittest.main()
