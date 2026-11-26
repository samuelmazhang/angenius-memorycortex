"""Unit tests for Cortex v6.5.0 consolidation pipeline (#A.2).

Covers the 5 dispatch-mandated tests + 8 helper / regression tests.

  Required (mapping to spec):
    1. test_supersede_detects_conflicting_value    -- SUPERSEDE path
    2. test_dedupe_detects_duplicate_fact         -- DEDUPE path
    3. test_keep_writes_new_chunk                 -- KEEP path
    4. test_conflict_log_records_action           -- audit row per action
    5. test_resolve_conflicts_is_idempotent       -- rerun converges

  Helpers / regression:
    6. test_values_equal_coerces_int_float
    7. test_values_equal_is_case_insensitive_str
    8. test_serialize_roundtrip
    9. test_deserialize_rejects_non_json
   10. test_resolve_handles_empty_input
   11. test_resolve_handles_facts_with_no_user_id
   12. test_resolve_handles_db_execute_failure_gracefully
   13. test_resolve_logs_action_even_when_insert_fails
   14. test_module_public_surface

Tests use an in-memory fake DB (FakeDB class) that mimics the schema
of ``memory_extracted_facts`` and ``memory_conflict_log``. No live
Postgres, no LLM, no httpx.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    ACTION_DEDUPE,
    ACTION_KEEP,
    ACTION_SUPERSEDE,
    VALID_ACTIONS,
    ConflictAction,
    resolve_conflicts,
)
from consolidate.conflict_resolve import (  # noqa: E402
    _canonical_key,
    _deserialize_fact,
    _serialize_fact,
    _values_equal,
)


# ===== In-memory DB fake =====

class FakeDB:
    """In-memory state for memory_extracted_facts + memory_conflict_log.

    Mimics the ``db_query`` / ``db_execute`` callables that the production
    code uses (``server.src.api_pg.query`` / ``.execute``).

    * SELECT against memory_extracted_facts: filters by user_id, returns
      rows newest-first by ``id`` (proxy for created_at).
    * INSERT into memory_extracted_facts: appends row, returns dict with
      ``{'id': new_id}`` (matches ``RETURNING id`` shape).
    * UPDATE memory_extracted_facts SET is_deprecated=TRUE: marks the row
      in-place. If the ``has_is_deprecated`` flag is False, raises so the
      caller can verify the swallow-and-log path.
    * INSERT into memory_conflict_log: appends to ``conflict_log`` list.

    The fake does NOT understand arbitrary SQL — it pattern-matches on a
    few canonical prefixes. That's enough for the conflict_resolve module,
    which only emits a small fixed set of SQL statements.
    """

    def __init__(self, *, has_is_deprecated: bool = True) -> None:
        self.facts: list = []  # rows of memory_extracted_facts
        self.conflict_log: list = []  # rows of memory_conflict_log
        self._next_id = 1
        self.has_is_deprecated = has_is_deprecated
        self.queries: list = []  # for assertion
        self.executes: list = []  # for assertion

    # ---- callables ----

    def query(self, sql, params=None, fetch="all"):
        self.queries.append((sql, params, fetch))
        normalized = " ".join((sql or "").split())
        if "FROM memory_extracted_facts" in normalized and "WHERE user_id" in normalized:
            user_id = (params or ("",))[0] or ""
            rows = [r for r in self.facts if r["user_id"] == user_id]
            rows = list(reversed(rows))  # newest first
            if fetch == "one":
                return rows[0] if rows else None
            return rows
        if "FROM memory_conflict_log" in normalized:
            return list(self.conflict_log)
        # Anything else → empty result (we don't simulate joins etc.)
        return []

    def execute(self, sql, params=None):
        self.executes.append((sql, params))
        normalized = " ".join((sql or "").split())

        if "INSERT INTO memory_extracted_facts" in normalized and "RETURNING id" in normalized:
            row = self._decode_insert_fact(params)
            row["id"] = self._next_id
            self._next_id += 1
            self.facts.append(row)
            return {"id": row["id"]}

        if "INSERT INTO memory_conflict_log" in normalized:
            log_row = {
                "user_id": params[0],
                "source_key": params[1],
                "old_chunk_id": params[2],
                "new_chunk_id": params[3],
                "action": params[4],
                "reason": params[5],
                "confidence": params[6],
                "decided_by": params[7],
            }
            self.conflict_log.append(log_row)
            return 1

        if "UPDATE memory_extracted_facts" in normalized and "is_deprecated" in normalized:
            if not self.has_is_deprecated:
                raise RuntimeError("column 'is_deprecated' does not exist")
            fact_id = params[0]
            for r in self.facts:
                if r["id"] == fact_id:
                    r["is_deprecated"] = True
                    r["deprecated_at"] = "now"
                    return 1
            return 0

        # Unknown — return rowcount 0
        return 0

    # ---- helpers ----

    def _decode_insert_fact(self, params):
        # _insert_new_fact: (source_chunk_id, fact_text, user_id, confidence)
        return {
            "source_chunk_id": params[0],
            "fact": params[1],
            "user_id": params[2],
            "confidence": params[3],
            "is_deprecated": False,
        }


def _fact(entity, predicate, value, confidence=0.9, user_id="u_001", source_chunk_id="c1"):
    """Convenience: build a chunk dict in extract()'s output shape."""
    return {
        "user_id": user_id,
        "source_chunk_id": source_chunk_id,
        "facts": [
            {"entity": entity, "predicate": predicate, "value": value, "confidence": confidence},
        ],
        "timeline": [],
        "preferences": [],
    }


def _seed(db, entity, predicate, value, confidence=0.9, user_id="u_001", source_chunk_id="c0"):
    """Insert a fact directly into the fake DB (bypassing resolve_conflicts)."""
    db.facts.append({
        "id": db._next_id,
        "source_chunk_id": source_chunk_id,
        "fact": _serialize_fact({"entity": entity, "predicate": predicate, "value": value, "confidence": confidence}),
        "user_id": user_id,
        "confidence": confidence,
        "is_deprecated": False,
    })
    db._next_id += 1


# ===== 1. SUPERSEDE detects conflicting value =====

def test_supersede_detects_conflicting_value():
    """New fact with same (entity, predicate) but different value → SUPERSEDE."""
    db = FakeDB()
    _seed(db, "Alice", "city", "Beijing", confidence=0.85)

    chunk = _fact("Alice", "city", "Shanghai", confidence=0.92)
    actions = resolve_conflicts([chunk], db.query, db.execute)

    assert len(actions) == 1
    a = actions[0]
    assert a.action == ACTION_SUPERSEDE
    assert a.entity == "Alice"
    assert a.predicate == "city"
    assert a.value == "Shanghai"
    assert a.old_fact_id == 1  # the seeded Beijing fact
    assert a.new_fact_id == 2  # the newly inserted Shanghai fact
    assert "supersede" in a.reason.lower()

    # Two facts in DB now: Beijing (deprecated) + Shanghai (active)
    assert len(db.facts) == 2
    assert db.facts[0]["is_deprecated"] is True
    assert db.facts[1]["is_deprecated"] is False

    # Audit row written
    assert len(db.conflict_log) == 1
    assert db.conflict_log[0]["action"] == "supersede"
    assert db.conflict_log[0]["old_chunk_id"] == "1"
    assert db.conflict_log[0]["new_chunk_id"] == "c1"


# ===== 2. DEDUPE detects duplicate fact =====

def test_dedupe_detects_duplicate_fact():
    """New fact with same (entity, predicate, value) → DEDUPE, no insert."""
    db = FakeDB()
    _seed(db, "Alice", "city", "Beijing", confidence=0.85)

    chunk = _fact("Alice", "city", "Beijing", confidence=0.95)
    actions = resolve_conflicts([chunk], db.query, db.execute)

    assert len(actions) == 1
    a = actions[0]
    assert a.action == ACTION_DEDUPE
    assert a.entity == "Alice"
    assert a.predicate == "city"
    assert a.value == "Beijing"
    assert a.old_fact_id == 1
    assert a.new_fact_id is None  # no INSERT

    # No new row inserted (still just the seeded Beijing)
    assert len(db.facts) == 1
    assert db.facts[0]["is_deprecated"] is False

    # Audit row still written
    assert len(db.conflict_log) == 1
    assert db.conflict_log[0]["action"] == "dedupe"


# ===== 3. KEEP writes new chunk =====

def test_keep_writes_new_chunk():
    """No existing fact in bucket → KEEP, insert new row."""
    db = FakeDB()  # empty
    chunk = _fact("Bob", "age", 30, confidence=0.88)
    actions = resolve_conflicts([chunk], db.query, db.execute)

    assert len(actions) == 1
    a = actions[0]
    assert a.action == ACTION_KEEP
    assert a.entity == "Bob"
    assert a.predicate == "age"
    assert a.value == 30
    assert a.old_fact_id is None
    assert a.new_fact_id == 1

    # One fact in DB
    assert len(db.facts) == 1
    assert db.facts[0]["is_deprecated"] is False
    # Fact text is JSON-encoded; roundtrip should give us our fields back
    parsed = _deserialize_fact(db.facts[0]["fact"])
    assert parsed == {"entity": "Bob", "predicate": "age", "value": 30, "confidence": 0.88}

    # Audit row written
    assert len(db.conflict_log) == 1
    assert db.conflict_log[0]["action"] == "keep"
    assert db.conflict_log[0]["new_chunk_id"] == "c1"


# ===== 4. conflict_log records action =====

def test_conflict_log_records_action():
    """Each call to resolve_conflicts writes exactly one audit row per fact,
    with action ∈ {keep, supersede, dedupe} and the right columns populated."""
    db = FakeDB()
    _seed(db, "Alice", "city", "Beijing", confidence=0.85)
    _seed(db, "Bob", "age", 30, confidence=0.90, user_id="u_001")
    # Note: the two seeded facts are in different buckets (entity+predicate differ)
    # so the new facts from the chunk below will exercise all 3 actions.

    chunks = [
        # KEEP — new entity/predicate pair, no existing
        _fact("Carol", "color", "blue", confidence=0.75),
        # DEDUPE — Alice+city+Beijing already exists
        _fact("Alice", "city", "Beijing", confidence=0.95),
        # SUPERSEDE — Bob+age+30 exists; new value 31 differs
        _fact("Bob", "age", 31, confidence=0.91),
    ]
    actions = resolve_conflicts(chunks, db.query, db.execute)

    assert [a.action for a in actions] == [ACTION_KEEP, ACTION_DEDUPE, ACTION_SUPERSEDE]
    assert len(db.conflict_log) == 3

    # KEEP row: new_chunk_id set, action=keep, no old_chunk_id
    keep_row = db.conflict_log[0]
    assert keep_row["action"] == "keep"
    assert keep_row["old_chunk_id"] is None
    assert keep_row["new_chunk_id"] == "c1"
    assert keep_row["source_key"] == "carol::color"
    assert keep_row["decided_by"] == "auto"

    # DEDUPE row: old_chunk_id set, no new chunk ref
    dedupe_row = db.conflict_log[1]
    assert dedupe_row["action"] == "dedupe"
    assert dedupe_row["old_chunk_id"] == "1"  # Alice+city seeded id
    assert dedupe_row["source_key"] == "alice::city"

    # SUPERSEDE row: both ids set, action=supersede
    supersede_row = db.conflict_log[2]
    assert supersede_row["action"] == "supersede"
    assert supersede_row["old_chunk_id"] == "2"  # Bob+age seeded id
    assert supersede_row["new_chunk_id"] == "c1"
    assert supersede_row["source_key"] == "bob::age"
    assert "supersede" in supersede_row["reason"].lower()


# ===== 5. resolve_conflicts is idempotent =====

def test_resolve_conflicts_is_idempotent():
    """Running resolve_conflicts twice on the same input converges:
    - first run may insert / supersede.
    - second run on the same input produces DEDUPE for every fact (no new inserts),
      so the effective DB state is stable.
    """
    db = FakeDB()
    _seed(db, "Alice", "city", "Beijing", confidence=0.85)

    chunk = _fact("Alice", "city", "Shanghai", confidence=0.92)

    actions1 = resolve_conflicts([chunk], db.query, db.execute)
    assert actions1[0].action == ACTION_SUPERSEDE
    rows_after_first = len(db.facts)
    log_after_first = len(db.conflict_log)

    # Second run with the same chunk → all DEDUPE, no new rows
    actions2 = resolve_conflicts([chunk], db.query, db.execute)
    assert all(a.action == ACTION_DEDUPE for a in actions2), (
        f"expected all DEDUPE on rerun, got {[a.action for a in actions2]}"
    )
    assert len(db.facts) == rows_after_first, "idempotency violated: new fact inserted"
    # Conflict log DOES grow (audit-trail-only is by design — every call logs)
    assert len(db.conflict_log) == log_after_first + len(actions2)

    # Third run: still all DEDUPE
    actions3 = resolve_conflicts([chunk], db.query, db.execute)
    assert all(a.action == ACTION_DEDUPE for a in actions3)
    assert len(db.facts) == rows_after_first


# ===== 6. _values_equal coerces int / float =====

def test_values_equal_coerces_int_float():
    """30 and 30.0 must be treated as equal (Python int(30) != float(30.0) is False,
    but our function explicitly normalizes them)."""
    assert _values_equal(30, 30.0)
    assert _values_equal(30.0, 30)
    assert not _values_equal(30, 31)
    assert not _values_equal(30.5, 30)
    # bool != int in Python (isinstance(True, int) is True) — we treat them as distinct.
    assert not _values_equal(True, 1)
    assert not _values_equal(False, 0)
    # None handling
    assert _values_equal(None, None)
    assert not _values_equal(None, 0)
    assert not _values_equal(0, None)


# ===== 7. _values_equal is case-insensitive for strings =====

def test_values_equal_is_case_insensitive_str():
    """'Beijing' == 'beijing ' should match (bucket key normalized)."""
    assert _values_equal("Beijing", "beijing")
    assert _values_equal("Beijing", "  BEIJING  ")
    assert not _values_equal("Beijing", "Shanghai")
    assert _values_equal("yes", "YES")


# ===== 8. Serialize roundtrip =====

def test_serialize_roundtrip():
    """_serialize_fact ↔ _deserialize_fact preserves entity / predicate / value / confidence."""
    cases = [
        {"entity": "Alice", "predicate": "city", "value": "Beijing", "confidence": 0.9},
        {"entity": "Bob", "predicate": "age", "value": 30, "confidence": 0.7},
        {"entity": "X", "predicate": "y", "value": None, "confidence": 0.0},
        {"entity": "Y", "predicate": "tags", "value": ["a", "b"], "confidence": 1.0},
    ]
    for f in cases:
        text = _serialize_fact(f)
        assert isinstance(text, str)
        parsed = _deserialize_fact(text)
        assert parsed is not None
        assert parsed["entity"] == f["entity"]
        assert parsed["predicate"] == f["predicate"]
        assert parsed["value"] == f["value"]
        assert float(parsed["confidence"]) == float(f["confidence"])


# ===== 9. _deserialize_fact rejects non-JSON / missing fields =====

def test_deserialize_rejects_non_json():
    """Non-JSON text or missing entity / predicate → None."""
    assert _deserialize_fact(None) is None
    assert _deserialize_fact("") is None
    assert _deserialize_fact("not json at all") is None
    assert _deserialize_fact('"just a string"') is None
    assert _deserialize_fact("[1, 2, 3]") is None
    assert _deserialize_fact('{"entity": "A"}') is None  # no predicate
    assert _deserialize_fact('{"predicate": "p"}') is None  # no entity
    assert _deserialize_fact('{"entity": "", "predicate": "p"}') is None  # empty entity


# ===== 10. Empty input =====

def test_resolve_handles_empty_input():
    """Empty / None / non-dict input → empty action list, no DB calls."""
    db = FakeDB()
    assert resolve_conflicts([], db.query, db.execute) == []
    assert resolve_conflicts(None, db.query, db.execute) == []
    # Each side-effect callable may be called zero times for empty input
    assert db.queries == []
    assert db.executes == []


# ===== 11. facts without user_id (global / empty) =====

def test_resolve_handles_facts_with_no_user_id():
    """Chunks missing user_id fall into the empty-user bucket and still produce actions."""
    db = FakeDB()
    chunk = {
        # no user_id, no source_chunk_id
        "facts": [{"entity": "X", "predicate": "y", "value": "z", "confidence": 0.8}],
    }
    actions = resolve_conflicts([chunk], db.query, db.execute)
    assert len(actions) == 1
    assert actions[0].action == ACTION_KEEP
    assert actions[0].user_id == ""
    # Source key is derived from entity/predicate even without user_id
    assert db.conflict_log[0]["source_key"] == "x::y"


# ===== 12. db_execute failure is graceful =====

def test_resolve_handles_db_execute_failure_gracefully():
    """If INSERT raises, resolve_conflicts must NOT propagate the exception;
    it logs a warning and returns a ConflictAction with new_fact_id=None."""
    class BrokenDB(FakeDB):
        def execute(self, sql, params=None):
            self.executes.append((sql, params))
            if "INSERT INTO memory_extracted_facts" in (sql or ""):
                raise RuntimeError("simulated DB outage")
            return super().execute(sql, params)

    db = BrokenDB()
    chunk = _fact("Alice", "city", "Beijing", confidence=0.9)
    actions = resolve_conflicts([chunk], db.query, db.execute)

    assert len(actions) == 1
    assert actions[0].action == ACTION_KEEP  # we still emit a KEEP with new_fact_id=None
    assert actions[0].new_fact_id is None


# ===== 13. Audit log still written when insert fails =====

def test_resolve_logs_action_even_when_insert_fails():
    """The conflict_log row must be written even if the INSERT fails —
    the audit trail is the source of truth for operator forensics."""
    class BrokenDB(FakeDB):
        def execute(self, sql, params=None):
            self.executes.append((sql, params))
            if "INSERT INTO memory_extracted_facts" in (sql or ""):
                raise RuntimeError("simulated DB outage")
            return super().execute(sql, params)

    db = BrokenDB()
    chunk = _fact("Alice", "city", "Beijing", confidence=0.9)
    resolve_conflicts([chunk], db.query, db.execute)

    # conflict_log INSERT succeeded even though fact INSERT failed
    assert len(db.conflict_log) == 1
    assert db.conflict_log[0]["action"] == "keep"


# ===== 14. Module public surface =====

def test_module_public_surface():
    """The package must expose the documented public API."""
    assert callable(resolve_conflicts)
    assert ACTION_KEEP == "KEEP"
    assert ACTION_SUPERSEDE == "SUPERSEDE"
    assert ACTION_DEDUPE == "DEDUPE"
    assert ACTION_KEEP in VALID_ACTIONS
    assert ACTION_SUPERSEDE in VALID_ACTIONS
    assert ACTION_DEDUPE in VALID_ACTIONS
    assert "ConflictAction" in dir(consolidate)

    # ConflictAction fields
    fields = ConflictAction.__dataclass_fields__
    for name in (
        "action", "user_id", "source_chunk_id", "entity", "predicate",
        "value", "confidence", "new_fact_id", "old_fact_id", "reason", "decided_at",
    ):
        assert name in fields, f"missing field: {name}"

    # source_key derivation
    ca = ConflictAction(
        action=ACTION_KEEP, user_id="u", source_chunk_id=None,
        entity="Alice", predicate="city", value="Beijing", confidence=0.9,
    )
    assert ca.source_key() == "alice::city"
    log = ca.to_log_dict()
    assert log["source_key"] == "alice::city"
    assert log["action"] == "keep"
    assert log["decided_by"] == "auto"


# ===== 15. Internal helper: _canonical_key =====

def test_canonical_key_normalizes_case_and_whitespace():
    """Bucket key should ignore leading / trailing whitespace and case."""
    assert _canonical_key("Alice", "City") == "alice::city"
    assert _canonical_key("  ALICE  ", " city ") == "alice::city"
    assert _canonical_key("", "") == "::"
    assert _canonical_key(None, "p") == "::p"


# ===== 16. SUPERSEDE tolerates missing is_deprecated column =====

def test_supersede_tolerates_missing_is_deprecated_column():
    """If memory_extracted_facts has no is_deprecated column (older schema),
    SUPERSEDE should still complete: log row written, new fact inserted,
    the failed UPDATE is swallowed at DEBUG level."""
    db = FakeDB(has_is_deprecated=False)
    _seed(db, "Alice", "city", "Beijing", confidence=0.85)

    chunk = _fact("Alice", "city", "Shanghai", confidence=0.92)
    actions = resolve_conflicts([chunk], db.query, db.execute)

    assert len(actions) == 1
    assert actions[0].action == ACTION_SUPERSEDE
    assert actions[0].new_fact_id == 2
    assert len(db.facts) == 2
    assert len(db.conflict_log) == 1
    assert db.conflict_log[0]["action"] == "supersede"


# ===== 17. Multiple facts in one chunk =====

def test_resolve_handles_multiple_facts_per_chunk():
    """One chunk with N facts → N actions, one per fact."""
    db = FakeDB()
    chunk = {
        "user_id": "u_001",
        "source_chunk_id": "c1",
        "facts": [
            {"entity": "A", "predicate": "p1", "value": 1, "confidence": 0.8},
            {"entity": "B", "predicate": "p2", "value": 2, "confidence": 0.7},
            {"entity": "C", "predicate": "p3", "value": 3, "confidence": 0.9},
        ],
    }
    actions = resolve_conflicts([chunk], db.query, db.execute)
    assert len(actions) == 3
    assert all(a.action == ACTION_KEEP for a in actions)
    assert len(db.facts) == 3
    assert len(db.conflict_log) == 3


# ===== 18. Non-dict / malformed entries are skipped, not crashed =====

def test_resolve_skips_malformed_entries():
    """Garbage input shouldn't crash resolve_conflicts."""
    db = FakeDB()
    chunks = [
        "not a dict",
        None,
        {},
        {"user_id": "u", "facts": "not a list"},
        {"user_id": "u", "facts": ["not a dict", None, {"entity": "A", "predicate": "p", "value": "v", "confidence": 0.5}]},
    ]
    actions = resolve_conflicts(chunks, db.query, db.execute)
    # Only the last nested dict is valid
    assert len(actions) == 1
    assert actions[0].entity == "A"


# ----- unittest mirror so ``python -m unittest`` also runs the suite -----

class _UnittestMirror(unittest.TestCase):
    def test_supersede_detects_conflicting_value(self):
        test_supersede_detects_conflicting_value()

    def test_dedupe_detects_duplicate_fact(self):
        test_dedupe_detects_duplicate_fact()

    def test_keep_writes_new_chunk(self):
        test_keep_writes_new_chunk()

    def test_conflict_log_records_action(self):
        test_conflict_log_records_action()

    def test_resolve_conflicts_is_idempotent(self):
        test_resolve_conflicts_is_idempotent()


if __name__ == "__main__":
    unittest.main()