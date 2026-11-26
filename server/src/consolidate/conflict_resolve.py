"""
Cortex v6.5.0 -- Consolidation Pipeline (#A.2): ADR-068 conflict resolution.

Purpose
-------
Given the structured facts emitted by ``extract()`` (the #A.1 layer) plus
two injected DB callables (``db_query``, ``db_execute``), apply ADR-068's
SUPERSEDE / DEDUPE / KEEP rules per (user_id, entity, predicate) bucket.

Decision matrix (M spec — cortex-v6.5-roadmap §5.2 Step 2):

  ┌──────────────────────────────────────────────────────────────────┐
  │ existing (user, entity, predicate)?   same value?   → action     │
  ├──────────────────────────────────────────────────────────────────┤
  │ no                                    n/a          → KEEP        │
  │ yes                                   yes          → DEDUPE      │
  │ yes                                   no           → SUPERSEDE   │
  └──────────────────────────────────────────────────────────────────┘

Side effects:
  * KEEP       — INSERT new row into ``memory_extracted_facts``
  * DEDUPE     — no DB row change; entry written to ``memory_conflict_log``
  * SUPERSEDE  — INSERT new row + best-effort mark old row deprecated
                 (only when the table has ``is_deprecated``; failure is
                 non-fatal because the ``memory_conflict_log`` row is
                 the authoritative audit record).

Every action emits exactly one ``memory_conflict_log`` row regardless of
outcome, so the audit table reflects 1:1 with the action list.

Design constraints:
  * No LLM calls — pure logic (dispatch constraint).
  * No external deps beyond stdlib.
  * All persistence is delegated to ``db_query`` / ``db_execute`` so the
    module is unit-testable without a live Postgres.
  * The ``fact`` column on ``memory_extracted_facts`` is plain ``TEXT``
    (per schema.sql). We JSON-encode the structured triple into it so
    downstream callers can parse it back. This keeps the change
    schema-additive (no new columns, no migration).
  * Idempotent: re-running ``resolve_conflicts`` on the same input
    converges to a stable set of effective rows. KEEP → DEDUPE on rerun;
    SUPERSEDE → DEDUPE on rerun (the new row already exists).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger("cortex.consolidate.conflict_resolve")


# ===== Action constants =====

ACTION_KEEP = "KEEP"
ACTION_SUPERSEDE = "SUPERSEDE"
ACTION_DEDUPE = "DEDUPE"

VALID_ACTIONS = frozenset({ACTION_KEEP, ACTION_SUPERSEDE, ACTION_DEDUPE})

# memory_conflict_log.action uses lowercase + ADR-068 vocabulary.
_LOG_ACTION_MAP: Dict[str, str] = {
    ACTION_SUPERSEDE: "supersede",
    ACTION_DEDUPE: "dedupe",
    ACTION_KEEP: "keep",
}

# How many candidate rows to load when scanning a user's bucket.
# Caps work even for power users with thousands of facts.
_FACT_SCAN_LIMIT = 200

# Soft cap on raw ``fact`` text length when serializing (defensive).
_MAX_FACT_TEXT = 8_000


# ===== Result dataclass =====

@dataclass
class ConflictAction:
    """One ADR-068 decision, suitable for logging and return to caller.

    Attributes
    ----------
    action : 'KEEP' | 'SUPERSEDE' | 'DEDUPE'
    user_id : str   (empty string → global user)
    source_chunk_id : Optional[str]   chunk that produced the new fact
    entity : str
    predicate : str
    value : Any    (JSON-serializable; str / int / float / bool / None / list)
    confidence : float    (new fact's confidence in [0, 1])
    new_fact_id : Optional[int]    PK after INSERT (None for DEDUPE)
    old_fact_id : Optional[int]    PK of the conflicting prior row
    reason : str    human-readable rationale (logged into conflict_log)
    decided_at : float    unix epoch when the decision was made
    """

    action: str
    user_id: str
    source_chunk_id: Optional[str]
    entity: str
    predicate: str
    value: Any
    confidence: float
    new_fact_id: Optional[int] = None
    old_fact_id: Optional[int] = None
    reason: str = ""
    decided_at: float = field(default_factory=lambda: time.time())

    def source_key(self) -> str:
        """Build the bucket identifier for ``memory_conflict_log.source_key``."""
        e = (self.entity or "").strip().lower()
        p = (self.predicate or "").strip().lower()
        return f"{e}::{p}"

    def to_log_dict(self) -> Dict[str, Any]:
        """Return a dict ready for ``INSERT INTO memory_conflict_log``."""
        return {
            "user_id": self.user_id or None,
            "source_key": self.source_key(),
            "old_chunk_id": str(self.old_fact_id) if self.old_fact_id is not None else None,
            "new_chunk_id": self.source_chunk_id,
            "action": _LOG_ACTION_MAP.get(self.action, "keep"),
            "reason": self.reason or "",
            "confidence": float(self.confidence) if self.confidence is not None else None,
            "decided_by": "auto",
        }


# ===== Pure helpers (no I/O) =====

def _canonical_key(entity: str, predicate: str) -> str:
    """Build the bucket key for matching. Case + whitespace insensitive."""
    e = (entity or "").strip().lower()
    p = (predicate or "").strip().lower()
    return f"{e}::{p}"


def _values_equal(a: Any, b: Any) -> bool:
    """Soft equality for conflict detection.

    Rules:
      * both None → equal
      * one None → not equal
      * numeric ints / floats → coerced to float (e.g. ``30 == 30.0``)
      * strings → case + whitespace insensitive (``"Beijing" == "beijing "``)
      * bool vs int (Python quirk) → not equal
      * everything else → ``==``
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    try:
        return a == b
    except Exception:  # noqa: BLE001 — defensive for unhashable
        return False


def _serialize_fact(fact: Dict[str, Any]) -> str:
    """JSON-encode a structured fact for the ``memory_extracted_facts.fact`` TEXT column."""
    payload = {
        "entity": fact.get("entity"),
        "predicate": fact.get("predicate"),
        "value": fact.get("value"),
        "confidence": fact.get("confidence"),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > _MAX_FACT_TEXT:
        # Defensive cap — anything bigger is probably an LLM hallucination.
        # Truncate value to keep payload small while keeping the bucket key.
        payload["value"] = "<truncated>"
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return text


def _deserialize_fact(text: str) -> Optional[Dict[str, Any]]:
    """Inverse of :func:`_serialize_fact`. Returns None for non-JSON / missing keys."""
    if not text or not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    entity = obj.get("entity")
    predicate = obj.get("predicate")
    if not entity or not predicate:
        return None
    return {
        "entity": str(entity),
        "predicate": str(predicate),
        "value": obj.get("value"),
        "confidence": obj.get("confidence"),
    }


# ===== I/O wrappers (pure delegation through db_query / db_execute) =====

def _find_existing_fact(
    user_id: str,
    entity: str,
    predicate: str,
    db_query: Callable[..., Any],
) -> Optional[Dict[str, Any]]:
    """Find the most recent active fact in the (user_id, entity, predicate) bucket.

    Returns ``None`` if no match. The returned dict carries ``id`` and the
    parsed ``value`` / ``confidence`` so the caller can decide DEDUPE vs
    SUPERSEDE without another round trip.

    Implementation note: we load the user's last ``_FACT_SCAN_LIMIT`` rows
    and filter in Python because ``memory_extracted_facts.fact`` is plain
    ``TEXT`` (no JSONB column to index on entity / predicate). The cap is
    intentionally generous; if a single user exceeds it, the oldest rows
    fall off the scan and we may re-insert them — acceptable because
    extract() is idempotent (temperature=0) and the schema's whole point
    is being human-readable, not optimized for this access pattern.
    """
    target_key = _canonical_key(entity, predicate)
    sql = (
        "SELECT id, source_chunk_id, fact, confidence, created_at "
        "FROM memory_extracted_facts "
        "WHERE user_id = %s "
        "ORDER BY created_at DESC "
        f"LIMIT {_FACT_SCAN_LIMIT}"
    )
    try:
        rows = db_query(sql, (user_id or "",), fetch="all")
    except Exception as exc:  # noqa: BLE001 — caller wants log + None
        logger.warning("conflict_resolve _find_existing_fact failed: %s", exc)
        return None
    if not rows:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _deserialize_fact(row.get("fact") or "")
        if parsed is None:
            continue
        if _canonical_key(parsed["entity"], parsed["predicate"]) != target_key:
            continue
        parsed["id"] = row.get("id")
        # V6.6.1 fix (2026-10-31): carry the originating chunk id so a
        # SUPERSEDE decision can be propagated back to memory_chunks.
        # Without it the conflict was only ever recorded against the fact
        # table and the stale chunk stayed active forever.
        parsed["source_chunk_id"] = row.get("source_chunk_id")
        # Prefer stored confidence; fall back to embedded one.
        conf = row.get("confidence")
        if conf is None:
            conf = parsed.get("confidence")
        try:
            parsed["confidence"] = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            parsed["confidence"] = 0.0
        return parsed
    return None


def _insert_new_fact(
    source_chunk_id: Optional[str],
    user_id: str,
    fact: Dict[str, Any],
    db_execute: Callable[..., Any],
) -> Optional[int]:
    """Insert one row into ``memory_extracted_facts``. Returns the new BIGSERIAL id."""
    fact_text = _serialize_fact(fact)
    confidence = fact.get("confidence")
    try:
        confidence_f = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_f = 0.0
    sql = (
        "INSERT INTO memory_extracted_facts "
        "(source_chunk_id, fact, user_id, confidence, created_at) "
        "VALUES (%s, %s, %s, %s, NOW()) "
        "RETURNING id"
    )
    try:
        result = db_execute(sql, (source_chunk_id, fact_text, user_id or None, confidence_f))
    except Exception as exc:  # noqa: BLE001
        logger.warning("conflict_resolve _insert_new_fact failed: %s", exc)
        return None
    # ``db_execute`` may return a row (with RETURNING) or a rowcount (without).
    if isinstance(result, dict):
        return result.get("id")
    if isinstance(result, (list, tuple)):
        if not result:
            return None
        first = result[0]
        if isinstance(first, dict):
            return first.get("id")
        if isinstance(first, (list, tuple)) and first:
            return first[0]
        return first
    return None


def _mark_old_deprecated(old_fact_id: int, db_execute: Callable[..., Any]) -> bool:
    """Best-effort: mark the old fact ``is_deprecated=TRUE``.

    The base ``memory_extracted_facts`` table has no ``is_deprecated`` column
    (see schema.sql). We attempt the UPDATE anyway — if the column doesn't
    exist we log at DEBUG and treat the log row as authoritative. This way
    the module is forward-compatible with a future schema bump.
    """
    sql = (
        "UPDATE memory_extracted_facts "
        "SET is_deprecated = TRUE, deprecated_at = NOW() "
        "WHERE id = %s"
    )
    try:
        db_execute(sql, (old_fact_id,))
        return True
    except Exception as exc:  # noqa: BLE001 — column may not exist
        logger.debug(
            "conflict_resolve _mark_old_deprecated no-op (column may be missing): %s", exc
        )
        return False


def _supersede_old_chunk(
    old_fact: Optional[Dict[str, Any]],
    new_source_chunk_id: Optional[str],
    new_confidence: float,
    db_query: Callable[..., Any],
    db_execute: Callable[..., Any],
) -> Optional[str]:
    """Propagate a SUPERSEDE decision back to ``memory_chunks``.

    V6.6.1 fix (2026-10-31).

    Why this exists
    ---------------
    The ADR-068 conflict layer only ever wrote to ``memory_extracted_facts``.
    The chunk that carried the outdated statement stayed
    ``is_deprecated = FALSE``, so it kept being served by ``/list`` and
    ``/search`` — and the arcadia connector injects ``/list`` straight into
    the agent's system prompt. The visible symptom was the agent repeatedly
    quoting a fact the user had already corrected.

    Safety rules (a wrong deprecation loses information, so we err hard
    towards NOT deprecating):

    1. The old fact must carry a ``source_chunk_id``.
    2. The old chunk must be a DIFFERENT chunk from the one that produced
       the new fact (never deprecate the chunk we just wrote).
    3. The old chunk must carry exactly ONE extracted fact. A chunk that
       holds several facts would lose the others if we deprecated it.
    4. The new fact must be at least as confident as the old one.
    5. The chunk row must still be live (``archived = 0`` and not already
       deprecated).

    Returns the deprecated chunk id, or None when nothing was changed.
    """
    if not old_fact:
        return None
    old_chunk_id = old_fact.get("source_chunk_id")
    if not old_chunk_id:
        return None
    if new_source_chunk_id and str(old_chunk_id) == str(new_source_chunk_id):
        return None

    try:
        old_conf = float(old_fact.get("confidence") or 0.0)
    except (TypeError, ValueError):
        old_conf = 0.0
    if float(new_confidence or 0.0) < old_conf:
        # The new statement is less trustworthy than the old one — keeping
        # the old fact alive is the safe outcome.
        return None

    try:
        rows = db_query(
            "SELECT COUNT(*) AS c FROM memory_extracted_facts "
            "WHERE source_chunk_id = %s",
            (old_chunk_id,),
            fetch="one",
        )
    except Exception as exc:  # noqa: BLE001 — never break the pipeline
        logger.debug("conflict_resolve chunk fact-count probe failed: %s", exc)
        return None
    if isinstance(rows, list):
        rows = rows[0] if rows else None
    if not isinstance(rows, dict):
        return None
    try:
        fact_count = int(rows.get("c") or 0)
    except (TypeError, ValueError):
        return None
    if fact_count > 1:
        # Multi-fact chunk: deprecating it would silently drop the other
        # facts it carries. Leave it alone.
        logger.debug(
            "conflict_resolve skip chunk deprecation: chunk %s carries %d facts",
            old_chunk_id, fact_count,
        )
        return None

    try:
        chunk = db_query(
            "SELECT id, content FROM memory_chunks "
            "WHERE id = %s AND archived = 0 AND is_deprecated = FALSE",
            (old_chunk_id,),
            fetch="one",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("conflict_resolve chunk probe failed: %s", exc)
        return None
    if isinstance(chunk, list):
        chunk = chunk[0] if chunk else None
    if not isinstance(chunk, dict) or not chunk.get("id"):
        return None

    try:
        db_execute(
            "UPDATE memory_chunks "
            "SET is_deprecated = TRUE, deprecated_at = NOW(), "
            "    superseded_by = %s, updated_at = NOW() "
            "WHERE id = %s AND archived = 0 AND is_deprecated = FALSE",
            (new_source_chunk_id, chunk.get("id")),
        )
    except Exception as exc:  # noqa: BLE001 — best effort, never fatal
        logger.warning(
            "conflict_resolve failed to deprecate superseded chunk %s: %s",
            chunk.get("id"), exc,
        )
        return None

    # Audit trail: the old text is preserved so a wrong deprecation is
    # recoverable by hand (and inspectable after the fact).
    try:
        hist_id = int(time.time() * 1e6) % 2000000000
        db_execute(
            "INSERT INTO memory_history(id, memory_id, action, content, type, created_at) "
            "VALUES (%s, %s, 'superseded', %s, 'supersede_log', NOW())",
            (hist_id, chunk.get("id"), chunk.get("content") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — audit is best effort
        logger.debug("conflict_resolve history write failed: %s", exc)

    logger.info(
        "conflict_resolve: deprecated superseded chunk %s (superseded_by=%s)",
        chunk.get("id"), new_source_chunk_id,
    )
    return str(chunk.get("id"))


def _log_conflict(action: ConflictAction, db_execute: Callable[..., Any]) -> bool:
    """Append one row to ``memory_conflict_log``. Returns True if written."""
    payload = action.to_log_dict()
    sql = (
        "INSERT INTO memory_conflict_log "
        "(user_id, source_key, old_chunk_id, new_chunk_id, "
        " action, reason, confidence, decided_by, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())"
    )
    params = (
        payload["user_id"],
        payload["source_key"],
        payload["old_chunk_id"],
        payload["new_chunk_id"],
        payload["action"],
        payload["reason"],
        payload["confidence"],
        payload["decided_by"],
    )
    try:
        db_execute(sql, params)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("conflict_resolve _log_conflict failed: %s", exc)
        return False


# ===== Per-fact decision =====

def _resolve_one_fact(
    chunk: Dict[str, Any],
    fact: Dict[str, Any],
    db_query: Callable[..., Any],
    db_execute: Callable[..., Any],
) -> ConflictAction:
    """Apply the M-spec decision matrix to a single extracted fact."""
    user_id = chunk.get("user_id") or ""
    source_chunk_id = chunk.get("source_chunk_id")
    entity = str(fact.get("entity") or "")
    predicate = str(fact.get("predicate") or "")
    value = fact.get("value")
    try:
        confidence = float(fact.get("confidence")) if fact.get("confidence") is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0

    # Look for an existing fact in the same bucket.
    existing = _find_existing_fact(user_id, entity, predicate, db_query)

    if existing is None:
        # No prior fact in this bucket → KEEP
        new_id = _insert_new_fact(source_chunk_id, user_id, fact, db_execute)
        action = ConflictAction(
            action=ACTION_KEEP,
            user_id=user_id,
            source_chunk_id=source_chunk_id,
            entity=entity,
            predicate=predicate,
            value=value,
            confidence=confidence,
            new_fact_id=new_id,
            old_fact_id=None,
            reason=f"no existing fact in bucket {entity!r}:{predicate!r}; insert id={new_id}",
        )
        _log_conflict(action, db_execute)
        return action

    if _values_equal(value, existing.get("value")):
        # Same bucket + same value → DEDUPE (no DB mutation, audit only)
        action = ConflictAction(
            action=ACTION_DEDUPE,
            user_id=user_id,
            source_chunk_id=source_chunk_id,
            entity=entity,
            predicate=predicate,
            value=value,
            confidence=confidence,
            new_fact_id=None,
            old_fact_id=existing.get("id"),
            reason=(
                f"duplicate of fact_id={existing.get('id')} "
                f"(old_conf={existing.get('confidence', 0.0):.2f})"
            ),
        )
        _log_conflict(action, db_execute)
        return action

    # Same bucket + different value → SUPERSEDE
    _mark_old_deprecated(existing.get("id"), db_execute)
    new_id = _insert_new_fact(source_chunk_id, user_id, fact, db_execute)
    # V6.6.1 fix (2026-10-31): propagate the supersede to the chunk that
    # carried the outdated statement. Until now the conflict was only
    # recorded in memory_extracted_facts, so the stale chunk stayed active
    # in memory_chunks and was still served by /list and /search — which is
    # exactly how an outdated fact kept re-entering the agent's prompt.
    _supersede_old_chunk(
        existing, source_chunk_id, confidence, db_query, db_execute
    )
    action = ConflictAction(
        action=ACTION_SUPERSEDE,
        user_id=user_id,
        source_chunk_id=source_chunk_id,
        entity=entity,
        predicate=predicate,
        value=value,
        confidence=confidence,
        new_fact_id=new_id,
        old_fact_id=existing.get("id"),
        reason=(
            f"new_conf={confidence:.2f} supersedes old_id={existing.get('id')} "
            f"(old_value={existing.get('value')!r}, old_conf={existing.get('confidence', 0.0):.2f})"
        ),
    )
    _log_conflict(action, db_execute)
    return action


# ===== Public entry =====

def resolve_conflicts(
    new_chunks: List[Dict[str, Any]],
    db_query: Callable[..., Any],
    db_execute: Callable[..., Any],
) -> List[ConflictAction]:
    """Apply ADR-068 SUPERSEDE / DEDUPE / KEEP rules to a batch of extracted facts.

    Parameters
    ----------
    new_chunks : list of dict
        Output shape produced by :func:`consolidate.extract.extract`:

        .. code-block:: python

           {
               "user_id": "u_001",
               "source_chunk_id": "chunk_xyz",   # optional
               "facts": [
                   {"entity": "Alice", "predicate": "city",
                    "value": "Beijing", "confidence": 0.92},
                   ...
               ],
               "timeline": [...],          # ignored by this layer
               "preferences": [...],       # ignored by this layer
           }

        ``timeline`` and ``preferences`` are passed through unchanged by
        ``extract()`` and intentionally ignored here — they don't carry
        (entity, predicate, value) triples and thus don't participate in
        the SUPERSEDE / DEDUPE matrix. #A.3 (scheduler) is free to extend
        this surface if those buckets grow a key.

    db_query : callable
        Signature ``(sql: str, params: tuple, fetch: str) -> Any``. The
        production implementation is ``server.src.api_pg.query``; tests
        inject an in-memory stub.

    db_execute : callable
        Signature ``(sql: str, params: tuple) -> Any``. Returns either
        a row (when SQL has ``RETURNING``) or a rowcount. Production:
        ``server.src.api_pg.execute``.

    Returns
    -------
    list[ConflictAction]
        One action per processed fact, in input order. Empty list if no
        facts. Each action also has a side-effecting row in
        ``memory_conflict_log`` (best-effort — failures are logged but
        do not raise, since the caller may be best-effort batch mode).

    Notes
    -----
    * Pure logic: no LLM, no network, no env read.
    * Idempotent: re-running on the same ``new_chunks`` converges to the
      same set of effective rows in ``memory_extracted_facts``. KEEP rows
      become DEDUPE on the second pass; SUPERSEDE rows become DEDUPE on
      the third pass (since the active row now matches the new value).
    * Any DB error is swallowed and surfaced as a ``None`` new_fact_id
      with a logged warning. The audit row is still attempted so
      operators can see what went wrong.
    """
    actions: List[ConflictAction] = []
    if not new_chunks:
        return actions
    for chunk in new_chunks:
        if not isinstance(chunk, dict):
            continue
        facts = chunk.get("facts")
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            try:
                action = _resolve_one_fact(chunk, fact, db_query, db_execute)
            except Exception as exc:  # noqa: BLE001 — never raise out of conflict_resolve
                logger.error(
                    "conflict_resolve _resolve_one_fact crashed: %s "
                    "(entity=%r predicate=%r)",
                    exc, fact.get("entity"), fact.get("predicate"),
                )
                # Emit a synthetic KEEP with no DB side-effects so the caller
                # still gets 1:1 row count and the audit log captures the error.
                action = ConflictAction(
                    action=ACTION_KEEP,
                    user_id=str(chunk.get("user_id") or ""),
                    source_chunk_id=chunk.get("source_chunk_id"),
                    entity=str(fact.get("entity") or ""),
                    predicate=str(fact.get("predicate") or ""),
                    value=fact.get("value"),
                    confidence=0.0,
                    new_fact_id=None,
                    old_fact_id=None,
                    reason=f"error in resolve: {exc}",
                )
                _log_conflict(action, db_execute)
            actions.append(action)
    return actions


__all__ = [
    "ACTION_KEEP",
    "ACTION_SUPERSEDE",
    "ACTION_DEDUPE",
    "VALID_ACTIONS",
    "ConflictAction",
    "resolve_conflicts",
]
