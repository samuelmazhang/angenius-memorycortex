"""Regression tests for the V6.6.0 origin binding and rerank-pool fixes.

Origin binding (task 4)
------------------------
Persistent agent memory is an attack channel. Measured in 2026: a
malicious instruction planted via one untrusted tool call fires in later
sessions with 55.5% success against OpenClaw and 81.7% against Claude
Code; one benchmark's sleeper implant still fired after 100 ordinary
sessions; and no tested system passes more than 1.5% of forget requests.

Prompt-level defences cut poisoning by at most 3.2 percentage points. What
works is binding the origin to the record AT WRITE TIME (attack success
0%), and making that binding TRAVEL with the record so consolidation
cannot launder it.

Cortex already had a `memory_history` audit trail, but it records *what
changed*, not *where the claim came from*, and it is not carried on the
record. So this adds:

  * memory_chunks.origin / origin_trust / origin_ref
  * the same three columns on memory_extracted_facts (the DERIVED facts),
    so the marker is inherited rather than dropped;
  * /provenance/audit, to answer "what in my memory did NOT come from
    the user?".

Rerank candidate pool (found while measuring)
----------------------------------------------
``candidates = ordered_ids[:max(limit, 10)]`` hard-capped the pool at 10
while the rerank scored ``candidates[:rerank_top_k]`` with rerank_top_k
defaulting to 30 — so 20 of the rerank's advertised slots were
unreachable. Measured on the LOCOMO corpus: the answer is inside the
vector channel's top-5 57.0% of the time, top-10 63.0%, top-30 75.0%,
top-60 76.0% — so ~12 points of answer mass sat in ranks 11..30 that the
reranker was never shown.

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

MOD = _load_api_pg()

from tests._router_source_bridge import api_source_with_routes  # noqa: E402

SOURCE = api_source_with_routes()


class TestResolveOrigin(unittest.TestCase):
    """The write-time origin policy."""

    def test_defaults_are_conservative(self):
        o, t, ref = MOD._resolve_origin(None, None, None)
        self.assertEqual(o, "agent")
        self.assertEqual(t, "derived",
                         "an unmarked write must not claim the user said it")
        self.assertIsNone(ref)

    def test_unknown_trust_level_is_rejected(self):
        with self.assertRaises(Exception):
            MOD._resolve_origin("agent", "super-trusted", None)

    def test_user_trust_requires_a_reference(self):
        """A 'the user said this' claim must be traceable."""
        with self.assertRaises(Exception):
            MOD._resolve_origin("user", "user", None)

    def test_external_trust_requires_a_reference(self):
        with self.assertRaises(Exception):
            MOD._resolve_origin("external", "external", None)

    def test_user_trust_with_reference_is_accepted(self):
        o, t, ref = MOD._resolve_origin("user", "user", "msg-42")
        self.assertEqual((o, t, ref), ("user", "user", "msg-42"))

    def test_derived_trust_needs_no_reference(self):
        o, t, ref = MOD._resolve_origin("agent", "derived", None)
        self.assertEqual(t, "derived")
        self.assertIsNone(ref)

    def test_origin_is_normalised(self):
        o, t, ref = MOD._resolve_origin("  TOOL  ", "derived", None)
        self.assertEqual(o, "tool")


class TestInheritOrigin(unittest.TestCase):
    """The anti-laundering rule: inherit, never upgrade."""

    def test_external_source_stays_external(self):
        o, t, ref = MOD._inherit_origin("external", "external", "http://x")
        self.assertEqual(t, "external",
                         "an external observation must not become a user claim")

    def test_user_source_is_inherited(self):
        o, t, ref = MOD._inherit_origin("user", "user", "msg-1")
        self.assertEqual(t, "user")
        self.assertEqual(ref, "msg-1")

    def test_unknown_source_falls_back_to_derived(self):
        o, t, ref = MOD._inherit_origin(None, None, None)
        self.assertEqual(t, "derived")

    def test_reference_is_carried(self):
        o, t, ref = MOD._inherit_origin("external", "external", "http://evil")
        self.assertEqual(ref, "http://evil",
                         "the derived fact must point back at its source")

    def test_most_distrustful_never_upgrades(self):
        self.assertEqual(MOD._most_distrustful("external", "user"), "external")
        self.assertEqual(MOD._most_distrustful("derived", "user"), "derived")
        self.assertEqual(MOD._most_distrustful("user", "user"), "user")

    def test_most_distrustful_handles_unknown(self):
        self.assertEqual(MOD._most_distrustful("bogus"), "derived")


class TestOriginWiring(unittest.TestCase):
    """The marker must be written on every insert path."""

    def test_chunk_insert_records_origin(self):
        self.assertIn(
            "INSERT INTO memory_chunks(id, content, type, tags, source, importance, "
            "metadata, embedding, tier, expires_at, model_version, origin, "
            "origin_trust, origin_ref, created_at, updated_at)",
            SOURCE,
        )

    def test_batch_insert_records_origin(self):
        self.assertIn(
            "INSERT INTO memory_chunks(id, content, type, tags, source, importance, "
            "metadata, embedding, tier, expires_at, model_version, origin, "
            "origin_trust, origin_ref, created_at, updated_at)",
            SOURCE,
        )

    def test_conflict_upsert_records_origin(self):
        seg = SOURCE[SOURCE.index("def conflict_upsert"):]
        seg = seg[: seg.index("\n@app.", 1)] if "\n@app." in seg else seg
        self.assertIn("origin, origin_trust, origin_ref", seg,
                      "the conflict resolver writes a decision and must record the origin")

    def test_extracted_facts_inherit_the_source_marker(self):
        self.assertIn(
            "INSERT INTO memory_extracted_facts(source_chunk_id, fact, user_id, "
            "confidence, origin, origin_trust, origin_ref, created_at)",
            SOURCE,
        )

    def test_extract_reads_the_source_chunks_marker(self):
        self.assertIn("SELECT origin, origin_trust, origin_ref FROM memory_chunks",
                      SOURCE,
                      "extraction must inherit from the source chunk, not from the caller")

    def test_search_results_expose_the_marker(self):
        self.assertIn('r["origin_trust"] = r.get("origin_trust")', SOURCE)

    def test_fetch_chunks_selects_the_marker(self):
        self.assertIn("origin, origin_trust, origin_ref", SOURCE)

    def test_audit_endpoint_exists(self):
        self.assertIn('@app.get("/provenance/audit")', SOURCE)

    def test_audit_route_is_declared_before_the_parameterised_one(self):
        """FastAPI matches routes in declaration order, so
        /provenance/{memory_id} would otherwise swallow "audit" as a
        memory id and return the single-chunk view instead of the audit
        listing. Measured live: /provenance/audit returned
        {"memory_id":"audit", ...} before the order was fixed."""
        audit_at = SOURCE.index('@app.get("/provenance/audit")')
        single_at = SOURCE.index('@app.get("/provenance/{memory_id}")')
        self.assertLess(audit_at, single_at,
                        "the audit route must be declared first")

    def test_provenance_returns_derived_facts(self):
        self.assertIn("derived_facts", SOURCE,
                      "the provenance view must show what was derived from the record")

    def test_schema_declares_the_columns(self):
        sql = _read_schema()
        if sql is None:
            self.skipTest("schema.sql not present in this layout")
        self.assertIn("origin_trust", sql)
        self.assertIn("origin_ref", sql)

    def test_migration_file_exists(self):
        # The directory the deploy script actually applies is
        # server/src/migrations/ (cortex-deploy.sh reads that path), not
        # server/docker/unified/migrations/.
        mig = os.path.join(SERVER_ROOT, "src", "migrations",
                           "2026_10_30_v6_6_0_origin_binding.sql")
        self.assertTrue(os.path.exists(mig), "the incremental migration must exist")


class TestRerankCandidatePool(unittest.TestCase):
    """The rerank must actually be able to see rerank_top_k candidates."""

    def test_pool_is_not_hard_capped_at_ten_when_reranking(self):
        self.assertIn(
            "_pool_size = max(req.limit, 10, req.rerank_top_k)",
            SOURCE,
            "the rerank pool must reach rerank_top_k",
        )

    def test_default_path_keeps_the_old_pool_size(self):
        self.assertIn("_pool_size = max(req.limit, 10)", SOURCE,
                      "without rerank the candidate set must be unchanged")

    def test_pool_is_widened_only_when_reranking(self):
        self.assertIn("if req.rerank and _rerank_enabled():", SOURCE)


if __name__ == "__main__":
    unittest.main()
