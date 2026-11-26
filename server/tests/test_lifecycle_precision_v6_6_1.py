"""Regression tests for the V6.6.1 lifecycle-precision fixes.

Audit findings from the hermes memory-retrieval incident (2026-10-31),
all traced to source rather than inferred:

1. **The prompt-injection channel had no lifecycle filter.**
   The hermes connector builds the frozen system-prompt snapshot from
   ``GET /list`` (cortex_memory_backend.list_entries). That endpoint
   filtered ONLY ``archived = 0``. ``is_deprecated`` (superseded by a
   newer fact) and ``expires_at`` (the TTL assigned by
   _tier_for_importance) were never consulted at read time — so a fact
   the user had already corrected was served again on every session
   start and injected straight into the system prompt.

2. **The terminal hydration step was not airtight.**
   ``_fetch_chunks`` is the last step of EVERY recall channel (vector /
   lexical / tag / raptor / graph expansion) and of /graph/khop. Its
   comment claimed it excluded "deleted/deprecated" chunks, but it only
   filtered ``archived``. Any id that slipped through a channel without
   a lifecycle filter came back anyway.

3. **The conflict pipeline never ran.**
   ``_resolve_conflict_for_chunk`` (the ADR-068 writer) requires
   ``metadata->>'source_key'``; the hermes connector sends no metadata
   at all, so its chunks land in no bucket. The periodic reconciliation
   scans ``WHERE metadata ? 'source_key'`` — also nothing. And the
   background scheduler was constructed WITHOUT ``extract_fn``, so
   ``_extract_one`` returned the empty envelope, resolve_conflicts saw
   zero facts, and nothing was EVER superseded. Even when it did fire,
   the background resolver only wrote to ``memory_extracted_facts`` —
   never back to ``memory_chunks``.

4. **The graph was re-enabled, exposing unfiltered traversal.**
   35c4f33 / 6068df6 / b3f17a5 turned relation mining from a no-op
   (it had been producing ZERO rows) into a dense corpus-wide graph.
   ``/graph/khop`` and ``/relations/batch`` traversed
   ``memory_relations`` raw, without joining the chunk table, so a
   superseded neighbour was reachable through the graph.

5. **The recall block duplicated the dump.**
   ``_load_from_cortex`` appended a "Memory Recall Context" block built
   from a FIXED, content-free query ("recent context") onto the same
   ``memory`` key that already held the whole /list dump. A fixed query
   returns roughly the same generic set on every boot, and the recall
   hits are a subset of the dump — so every matching fact was injected
   TWICE into the same system prompt.

These tests use stubs — no Postgres, no LLM, no graph libraries.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _find_repo_root() -> str:
    """Locate the tree that holds connector/hermes.

    The tests live at <repo>/server/tests/ locally, so the repo root is
    two levels up. In the deployed container the tree is flattened to
    /app/tests/, so the repo root is ONE level up -- and the old fixed
    "two levels up" resolved to the filesystem root, where
    connector/hermes does not exist. That raised FileNotFoundError at
    COLLECTION time, which aborted the whole pytest run rather than
    just this file.

    The deploy copies connector/hermes into the container, so both
    layouts are complete; the lookup just has to find the right one.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", ".."), os.path.join(here, "..")):
        cand = os.path.abspath(cand)
        if os.path.isdir(os.path.join(cand, "connector", "hermes")):
            return cand
    return os.path.abspath(os.path.join(here, "..", ".."))


REPO_ROOT = _find_repo_root()

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from consolidate import conflict_resolve as _CR  # noqa: E402


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


from tests._router_source_bridge import api_source_with_routes  # noqa: E402

SOURCE = api_source_with_routes()
CONFLICT_SRC = _read(os.path.join(SRC_DIR, "consolidate", "conflict_resolve.py"))
RAPTOR_SRC = _read(os.path.join(SRC_DIR, "consolidate", "raptor_index.py"))
SCHEDULER_SRC = _read(os.path.join(SRC_DIR, "consolidate", "scheduler.py"))
HERMES_TOOL_SRC = _read(os.path.join(REPO_ROOT, "connector", "hermes", "memory_tool.py"))
HERMES_BACKEND_SRC = _read(
    os.path.join(REPO_ROOT, "connector", "hermes", "cortex_memory_backend.py")
)
_API_SRC = SOURCE


def _load_api_function(name: str, extra_globals: dict):
    """Compile a single function out of api_pg.py and return it.

    api_pg.py cannot be imported here (it needs psycopg2 / fastapi), so
    the function is extracted with the AST and compiled in isolation with
    a stub ``query`` / ``execute`` / ``logger`` / ``time`` in scope.
    That keeps the behavioural test honest -- it exercises the REAL
    source of the function under test, not a re-implementation.
    """
    tree = ast.parse(SOURCE)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            fn = node
            break
    if fn is None:
        raise AssertionError(f"api_pg.py has no function named {name!r}")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {
        "List": list,
        "Any": object,
        "Optional": object,
        "Callable": object,
        "Dict": dict,
        "logger": _StubLogger(),
        "time": _StubTime(),
        "NEAR_DUP_SIMILARITY": 0.93,
        "NEAR_DUP_WINDOW": 200,
    }
    ns.update(extra_globals)
    exec(compile(module, "<api_pg-extract>", "exec"), ns)
    return ns[name], ns


class _StubLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _StubTime:
    @staticmethod
    def time() -> float:
        return 1700000000.0


_API_FN, _API_NS = _load_api_function("_supersede_near_duplicates", {})


def _slice(source: str, start: str, end: str) -> str:
    """Return the source between two markers (exclusive of the end marker)."""
    i = source.index(start)
    j = source.index(end, i)
    return source[i:j]


def _call_sites_of(source: str, attr: str) -> list:
    """Return the line numbers where ``self.<attr>`` is referenced in CODE.

    Docstrings and comments are ignored, so a prose mention of a
    predicate does not count as a call site.
    """
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            lines.append(node.lineno)
    return lines


def _if_gates_of(source: str, attr: str) -> list:
    """Return the line numbers of ``if`` statements gated on ``self.<attr>``."""
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr == attr
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "self"
            ):
                lines.append(node.lineno)
                break
    return lines


class TestListEndpointLifecycle(unittest.TestCase):
    """GET /list is what hermes injects into the system prompt.

    It must hide superseded and expired facts by default, and expose
    them only through an explicit opt-in (the hermes duplicate check
    needs to see a deprecated entry so it does not re-create it).
    """

    def setUp(self):
        self.seg = _slice(SOURCE, "def list_chunks(", "# === Search ===")

    def test_list_hides_deprecated_by_default(self):
        self.assertIn('where.append("is_deprecated = FALSE")', self.seg)
        self.assertIn("include_deprecated: bool = False", self.seg)

    def test_list_hides_expired_by_default(self):
        self.assertIn(
            'where.append("(expires_at IS NULL OR expires_at > NOW())")', self.seg
        )
        self.assertIn("include_expired: bool = False", self.seg)

    def test_list_lifecycle_filters_apply_to_both_count_and_page(self):
        # The filters live in the shared `where` list, so they must be
        # appended BEFORE count_sql / sql are built.
        self.assertLess(
            self.seg.index('where.append("is_deprecated = FALSE")'),
            self.seg.index("count_sql ="),
            "the lifecycle filter must be part of the shared where list",
        )

    def test_list_still_filters_archived(self):
        self.assertIn("archived = 0", self.seg)


class TestFinalHydrationIsAirtight(unittest.TestCase):
    """_fetch_chunks is the last gate before a row reaches the caller.

    If it does not filter the lifecycle, then every channel that forgets
    to filter leaks a superseded fact — which is exactly what happened.
    """

    def setUp(self):
        self.seg = _slice(SOURCE, "def _fetch_chunks(", "def _expand_graph_neighbors(")

    def test_fetch_chunks_filters_deprecated(self):
        self.assertIn("is_deprecated = FALSE", self.seg)

    def test_fetch_chunks_filters_expired(self):
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", self.seg)

    def test_fetch_chunks_still_filters_archived(self):
        self.assertIn("archived = 0", self.seg)


class TestEveryChannelFiltersLifecycle(unittest.TestCase):
    """A channel that forgets the filter must not be able to leak."""

    def test_tag_recall_channel_filters_lifecycle(self):
        seg = _slice(SOURCE, 'tag_conds = " OR ".join(', 'rankings["tag"] = tag_ids')
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", seg)

    def test_vector_only_path_filters_lifecycle(self):
        seg = _slice(SOURCE, "def _search_vector_only(", '@app.get("/search/tts")')
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", seg)

    def test_hybrid_base_where_filters_expiry(self):
        seg = _slice(SOURCE, "base_where = [", "where_sql = ")
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", seg)

    def test_graph_expansion_legacy_fallback_filters_lifecycle(self):
        seg = _slice(
            SOURCE,
            "def _expand_graph_neighbors_filtered(",
            "def _graph_ppr_enabled(",
        )
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", seg)


class TestGraphTraversalFiltersLifecycle(unittest.TestCase):
    """The graph was re-enabled at scale; its traversal must filter too.

    /graph/khop and /relations/batch used to read memory_relations raw,
    so a superseded neighbour was reachable even though every direct
    channel filtered it.
    """

    def test_graph_khop_joins_the_chunk_table(self):
        seg = _slice(SOURCE, "def graph_khop(", "def _hop_of(")
        self.assertIn("JOIN memory_chunks c1", seg)
        self.assertIn("JOIN memory_chunks c2", seg)
        self.assertIn("c1.is_deprecated = FALSE", seg)
        self.assertIn("c2.is_deprecated = FALSE", seg)

    def test_relations_batch_joins_the_chunk_table(self):
        seg = _slice(SOURCE, "def get_relations_batch(", "rows = query(sql, params)")
        self.assertIn("JOIN memory_chunks", seg)
        self.assertIn("is_deprecated = FALSE", seg)
        # The recursive branch walks through `walk` — it must filter there
        # too, otherwise depth >= 2 still walks through dead nodes.
        self.assertIn("cn.is_deprecated = FALSE", seg)
        self.assertIn("ce.is_deprecated = FALSE", seg)


class TestRaptorTreeExcludesDeadChunks(unittest.TestCase):
    """A deprecated chunk baked into the summary tree stays reachable."""

    def test_rebuild_filters_lifecycle(self):
        seg = _slice(RAPTOR_SRC, "def rebuild(", "if not rows:")
        self.assertIn("is_deprecated = FALSE", seg)
        self.assertIn("(expires_at IS NULL OR expires_at > NOW())", seg)


class TestConflictPipelineCanMarkChunksStale(unittest.TestCase):
    """The conflict machinery must reach memory_chunks, not just the fact table.

    Until this fix the only writers of memory_chunks.is_deprecated were
    /conflict/upsert and /conflict/scan, and BOTH require a
    metadata->>'source_key' that the hermes connector never sends.
    """

    def test_supersede_propagates_to_the_chunk(self):
        self.assertIn("def _supersede_old_chunk(", CONFLICT_SRC)
        self.assertIn("UPDATE memory_chunks", CONFLICT_SRC)
        self.assertIn("is_deprecated = TRUE", CONFLICT_SRC)
        self.assertIn("superseded_by = %s", CONFLICT_SRC)

    def test_supersede_is_called_from_the_decision_path(self):
        self.assertIn("_supersede_old_chunk(", CONFLICT_SRC)
        seg = _slice(CONFLICT_SRC, "# Same bucket + different value", "action = ConflictAction(")
        self.assertIn("_supersede_old_chunk(", seg)

    def test_supersede_is_conservative(self):
        # A chunk carrying several facts must NOT be deprecated wholesale,
        # and the new statement must be at least as trustworthy.
        seg = _slice(CONFLICT_SRC, "def _supersede_old_chunk(", "def _log_conflict(")
        self.assertIn("fact_count > 1", seg)
        self.assertIn("float(new_confidence or 0.0) < old_conf", seg)
        self.assertIn("archived = 0 AND is_deprecated = FALSE", seg)

    def test_supersede_writes_an_audit_trail(self):
        seg = _slice(CONFLICT_SRC, "def _supersede_old_chunk(", "def _log_conflict(")
        self.assertIn("INSERT INTO memory_history", seg)

    def test_fact_lookup_carries_the_source_chunk(self):
        seg = _slice(CONFLICT_SRC, "def _find_existing_fact(", "def _insert_new_fact(")
        self.assertIn("source_chunk_id", seg)


class TestSchedulerWiring(unittest.TestCase):
    """The detection half of the pipeline was never connected.

    Without extract_fn, _extract_one short-circuits to the EMPTY
    envelope, resolve_conflicts sees zero facts and returns zero
    actions — so nothing was ever superseded.
    """

    def test_scheduler_receives_an_extract_function(self):
        seg = _slice(SOURCE, "_scheduler = _ConsolidationSchedulerImpl(", "on_process_complete=")
        self.assertIn("extract_fn=(", seg)

    def test_extract_bridge_is_defined(self):
        self.assertIn("def _sched_extract_fn(", SOURCE)
        self.assertIn("asyncio.run(", SOURCE)

    def test_extract_bridge_probes_the_llm_before_trying(self):
        # extract() retries 3x with a 30s/60s backoff; a down LLM must
        # not wedge the consolidation worker.
        self.assertIn("def _llm_endpoint_reachable(", SOURCE)
        seg = _slice(SOURCE, "def _sched_extract_fn(", "_scheduler = _ConsolidationSchedulerImpl(")
        self.assertIn("_llm_endpoint_reachable(", seg)

    def test_extract_never_raises_out_of_the_worker(self):
        seg = _slice(SOURCE, "def _sched_extract_fn(", "_scheduler = _ConsolidationSchedulerImpl(")
        self.assertIn("except Exception", seg)

    def test_scheduler_short_circuits_without_extract_fn(self):
        # Guard the invariant in the scheduler itself: a missing extract_fn
        # must yield an empty envelope, never an exception.
        seg = _slice(SCHEDULER_SRC, "def _extract_one(", "def _load_state(")
        self.assertIn("if self._extract_fn is None:", seg)
        self.assertIn("return base", seg)


class TestHermesSnapshotHasNoDuplicates(unittest.TestCase):
    """The recall block must not duplicate the /list dump."""

    def test_no_hardcoded_generic_recall_query(self):
        self.assertNotIn(
            'question="recent context"',
            HERMES_TOOL_SRC,
            "a fixed, content-free query re-injects the same stale set every boot",
        )

    def test_active_recall_is_opt_in(self):
        self.assertIn('os.getenv("HERMES_ACTIVE_RECALL", "0")', HERMES_TOOL_SRC)
        self.assertIn('os.getenv("HERMES_RECALL_QUERY", "")', HERMES_TOOL_SRC)

    def test_recall_block_is_deduplicated_against_the_dump(self):
        seg = _slice(HERMES_TOOL_SRC, "if getattr(self, \"_recall_context\", None):", "return {")
        self.assertIn("_norm_for_dedupe", seg)
        self.assertIn("_seen", seg)

    def test_dedupe_helper_normalizes_whitespace_and_case(self):
        self.assertIn("def _norm_for_dedupe(", HERMES_TOOL_SRC)
        seg = _slice(
            HERMES_TOOL_SRC,
            "def _norm_for_dedupe(",
            "# FIX (2026-08-02, Direction C): three-tier memory",
        )
        self.assertIn(".lower()", seg)

    def test_snapshot_entry_count_is_capped(self):
        self.assertIn("def _snapshot_max_entries(", HERMES_TOOL_SRC)
        self.assertIn("HERMES_SNAPSHOT_MAX_ENTRIES", HERMES_TOOL_SRC)
        seg = _slice(
            HERMES_TOOL_SRC,
            "# Net savings: 39 fewer HTTP roundtrips per load",
            "# Capture frozen snapshot",
        )
        self.assertIn("_snapshot_max_entries()", seg)
        # The legacy .md path must stay untouched: it is already bounded by
        # its write-time char budget.
        md_seg = _slice(
            HERMES_TOOL_SRC,
            'self.memory_entries = self._read_file(mem_dir / "MEMORY.md")',
            "def _load_from_cortex(",
        )
        self.assertNotIn("_snapshot_max_entries()", md_seg)

    def test_cortex_takeover_gate_is_not_the_inverted_predicate(self):
        # The committed code read `if self._is_cortex_unavailable(): ->
        # load from cortex`, i.e. the inverted meaning: it loaded from
        # cortex when cortex was DOWN. The gate must be `self.use_cortex`.
        # Checked on the AST so a mention inside a docstring does not
        # count as a call site.
        self.assertEqual(
            _if_gates_of(HERMES_TOOL_SRC, "_is_cortex_unavailable"), [],
            "no branch may be gated on the inverted predicate",
        )
        self.assertGreaterEqual(
            len(_if_gates_of(HERMES_TOOL_SRC, "use_cortex")), 5,
            "the cortex takeover gate must be `if self.use_cortex:`",
        )


class TestHermesBackendListContract(unittest.TestCase):
    """The connector must opt in to deprecated rows only where it needs them."""

    def test_list_entries_defaults_to_hiding_deprecated(self):
        seg = _slice(HERMES_BACKEND_SRC, "def list_entries(", "def search(")
        self.assertIn("include_deprecated: bool = False", seg)
        self.assertIn("include_deprecated", seg)

    def test_duplicate_check_sees_deprecated_rows(self):
        seg = _slice(HERMES_BACKEND_SRC, "def find_existing_chunk(", "def create_entry(")
        self.assertIn("include_deprecated=True", seg)

    def test_substring_lookup_sees_deprecated_rows(self):
        seg = _slice(HERMES_BACKEND_SRC, "def lookup_entry_by_substring(", "def is_cortex_active(")
        self.assertIn("include_deprecated=True", seg)


class _ChunkDB:
    """Minimal fake for the chunk-level supersede write-back."""

    def __init__(self, chunks=None, facts=None):
        self.chunks = chunks or {}   # chunk_id -> {"content": str, "live": bool}
        self.facts = facts or []      # [{"source_chunk_id": str}]
        self.executed = []

    def query(self, sql, params=None, fetch="all"):
        params = params or ()
        if "COUNT(*)" in sql:
            cid = params[0]
            return {"c": sum(1 for f in self.facts if f["source_chunk_id"] == cid)}
        if "FROM memory_chunks" in sql:
            cid = params[0]
            row = self.chunks.get(cid)
            if not row or not row.get("live", True):
                return None
            return {"id": cid, "content": row.get("content", "")}
        return []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return 1

    def update_statements(self):
        return [s for s, _ in self.executed if s.startswith("UPDATE memory_chunks")]


class TestSupersedeOldChunkBehaviour(unittest.TestCase):
    """Behavioural tests for the chunk-level supersede write-back.

    This is the piece that actually stops an outdated fact from being
    served again: the ADR-068 conflict layer used to write only to
    memory_extracted_facts, leaving the chunk itself active.
    """

    def _call(self, db, old_fact, new_chunk_id="chunk_new", new_confidence=0.9):
        return _CR._supersede_old_chunk(
            old_fact, new_chunk_id, new_confidence, db.query, db.execute
        )

    def test_single_fact_chunk_is_deprecated(self):
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "用户住在北京", "live": True}},
            facts=[{"source_chunk_id": "chunk_old"}],
        )
        old_fact = {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.7}
        self.assertEqual(self._call(db, old_fact), "chunk_old")
        updates = db.update_statements()
        self.assertEqual(len(updates), 1)
        self.assertIn("is_deprecated = TRUE", updates[0])
        self.assertIn("superseded_by = %s", updates[0])

    def test_deprecation_points_at_the_new_chunk(self):
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "x", "live": True}},
            facts=[{"source_chunk_id": "chunk_old"}],
        )
        self._call(db, {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.7})
        params = [p for s, p in db.executed if s.startswith("UPDATE memory_chunks")][0]
        self.assertEqual(params[0], "chunk_new")
        self.assertEqual(params[1], "chunk_old")

    def test_multi_fact_chunk_is_left_alone(self):
        # Deprecating it would silently drop the other facts it carries.
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "x", "live": True}},
            facts=[
                {"source_chunk_id": "chunk_old"},
                {"source_chunk_id": "chunk_old"},
            ],
        )
        old_fact = {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.7}
        self.assertIsNone(self._call(db, old_fact))
        self.assertEqual(db.update_statements(), [])

    def test_same_chunk_is_never_deprecated(self):
        db = _ChunkDB(
            chunks={"chunk_a": {"content": "x", "live": True}},
            facts=[{"source_chunk_id": "chunk_a"}],
        )
        old_fact = {"id": 1, "source_chunk_id": "chunk_a", "confidence": 0.7}
        self.assertIsNone(self._call(db, old_fact, new_chunk_id="chunk_a"))
        self.assertEqual(db.update_statements(), [])

    def test_less_confident_new_fact_does_not_win(self):
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "x", "live": True}},
            facts=[{"source_chunk_id": "chunk_old"}],
        )
        old_fact = {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.95}
        self.assertIsNone(self._call(db, old_fact, new_confidence=0.4))
        self.assertEqual(db.update_statements(), [])

    def test_dead_chunk_is_not_deprecated_again(self):
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "x", "live": False}},
            facts=[{"source_chunk_id": "chunk_old"}],
        )
        old_fact = {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.7}
        self.assertIsNone(self._call(db, old_fact))
        self.assertEqual(db.update_statements(), [])

    def test_fact_without_source_chunk_is_skipped(self):
        db = _ChunkDB(chunks={"chunk_old": {"content": "x", "live": True}})
        self.assertIsNone(self._call(db, {"id": 1, "confidence": 0.7}))
        self.assertEqual(db.update_statements(), [])

    def test_history_row_is_written_for_audit(self):
        db = _ChunkDB(
            chunks={"chunk_old": {"content": "用户住在北京", "live": True}},
            facts=[{"source_chunk_id": "chunk_old"}],
        )
        self._call(db, {"id": 1, "source_chunk_id": "chunk_old", "confidence": 0.7})
        history = [s for s, _ in db.executed if "INSERT INTO memory_history" in s]
        self.assertEqual(len(history), 1)


class _NearDupDB:
    """Fake for the near-duplicate suppression scan.

    ``similar`` maps chunk_id -> similarity score; only chunks listed
    there are returned by the similarity query, mimicking pgvector.
    """

    def __init__(self, similar=None):
        self.similar = similar or {}   # chunk_id -> {"importance": int}
        self.executed = []

    def query(self, sql, params=None, fetch="all"):
        params = params or ()
        if "1 - (c.embedding <=>" in sql:
            rows = []
            for cid, info in self.similar.items():
                if cid == params[3]:   # the new chunk is excluded
                    continue
                rows.append({"id": cid, "importance": info.get("importance", 5)})
            return rows
        return []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return 1

    def update_statements(self):
        return [s for s, _ in self.executed if s.startswith("UPDATE memory_chunks")]


class TestNearDuplicateSuppression(unittest.TestCase):
    """Behavioural tests for the LLM-free near-duplicate suppression.

    The ADR-068 conflict layer needs an LLM to decide that one fact
    supersedes another. When the LLM endpoint is down, nothing marks the
    old fact, so two near-identical statements stay active and BOTH get
    injected into the agent's system prompt. This is the fallback.
    """

    def _call(self, db, new_id="chunk_new", source="hermes_memory",
              importance=7, emb="[0.1,0.2]", model="nomic-v1.5"):
        # Re-bind the module-level query/execute the function closes over
        # to this test's stub, then run the REAL function body.
        fn, _ns = _load_api_function(
            "_supersede_near_duplicates",
            {"query": db.query, "execute": db.execute},
        )
        return fn(new_id, source, importance, emb, model)

    def test_near_duplicate_is_deprecated(self):
        db = _NearDupDB({"chunk_old": {"importance": 7}})
        result = self._call(db)
        self.assertEqual(result, ["chunk_old"])
        self.assertEqual(len(db.update_statements()), 1)

    def test_update_sets_the_supersede_columns(self):
        db = _NearDupDB({"chunk_old": {"importance": 7}})
        self._call(db)
        sql, params = db.executed[0]
        self.assertIn("is_deprecated = TRUE", sql)
        self.assertIn("superseded_by", sql)
        self.assertIn("WHERE id = %s AND archived = 0 AND is_deprecated = FALSE", sql)
        self.assertEqual(params, ("chunk_new", "chunk_old"))

    def test_more_important_old_fact_is_kept(self):
        # The new statement is weaker -- keeping the old fact is the safe
        # outcome (mirrors the conflict resolver).
        db = _NearDupDB({"chunk_old": {"importance": 9}})
        self.assertEqual(self._call(db, importance=4), [])
        self.assertEqual(db.update_statements(), [])

    def test_equal_importance_is_superseded(self):
        db = _NearDupDB({"chunk_old": {"importance": 7}})
        self.assertEqual(self._call(db, importance=7), ["chunk_old"])

    def test_history_row_is_written_for_audit(self):
        db = _NearDupDB({"chunk_old": {"importance": 7}})
        self._call(db)
        history = [s for s, _ in db.executed if "INSERT INTO memory_history" in s]
        self.assertEqual(len(history), 1)

    def test_scan_filters_on_source_and_lifecycle(self):
        # The SQL itself must constrain the candidate set: same source,
        # not archived, not already deprecated.
        sql = _API_SRC[_API_SRC.index("def _supersede_near_duplicates"):]
        sql = sql[:sql.index("\ndef ")]
        self.assertIn("c.source = %s", sql)
        self.assertIn("c.archived = 0", sql)
        self.assertIn("c.is_deprecated = FALSE", sql)
        self.assertIn("NEAR_DUP_SIMILARITY", sql)

    def test_scan_is_best_effort_and_never_raises(self):
        class _BrokenDB:
            def query(self, *a, **k):
                raise RuntimeError("db down")

            def execute(self, *a, **k):
                raise RuntimeError("db down")

        self.assertEqual(self._call(_BrokenDB()), [])

    def test_threshold_is_conservative(self):
        # A loose threshold would deprecate merely-related facts, which
        # loses information. 0.93 is tight on purpose.
        self.assertGreaterEqual(_API_NS["NEAR_DUP_SIMILARITY"], 0.90)


if __name__ == "__main__":
    unittest.main()
