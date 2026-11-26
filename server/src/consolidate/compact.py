"""Cortex v6.5.0 -- Consolidation Pipeline (#C): long-conversation compaction.

See module docstring as the API contract.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from .summary import (  # noqa: F401
    CHARS_PER_TOKEN,
    DEFAULT_TEMPLATE,
    summarize_chunks,
)
from .summary import _approx_tokens, _format_line, _normalize_chunk


logger = logging.getLogger("cortex.consolidate.compact")


DEFAULT_IMPORTANCE_THRESHOLD = 6.0
DEDUPE_OVERLAP_CHARS = 16


@dataclass
class CompactionResult:
    """One compaction outcome."""

    summary: str
    summary_tokens: int
    kept_chunk_ids: List[str]
    dropped_chunk_ids: List[str]
    skipped_chunk_ids: List[str]
    key_facts_preserved: List[str]
    source_chunk_id: str
    importance: float
    persisted: bool
    persisted_skipped_reason: str = ""
    compressed_id: Optional[int] = None
    decided_at: float = field(default_factory=lambda: time.time())

    def to_dict(self):
        return asdict(self)


def _content_key(chunk):
    raw = chunk.get("content") or ""
    return " ".join(str(raw).lower().split())


def _content_overlap_key(chunk):
    return _content_key(chunk)[:DEDUPE_OVERLAP_CHARS]


def _dedupe_chunks(chunks):
    by_key = {}
    by_overlap = {}

    for c in chunks:
        key = _content_key(c)
        ov = _content_overlap_key(c)
        if key in by_key:
            existing = by_key[key]
            if float(c.get("importance") or 0) > float(existing.get("importance") or 0):
                by_key[key] = c
            continue
        if ov in by_overlap:
            prior_key = by_overlap[ov]
            existing = by_key.get(prior_key)
            if existing is None:
                by_key[key] = c
                by_overlap[ov] = key
                continue
            if float(c.get("importance") or 0) > float(existing.get("importance") or 0):
                del_idx = None
                for k, v in list(by_key.items()):
                    if v is existing:
                        del_idx = k
                        break
                if del_idx is not None:
                    by_key.pop(del_idx, None)
                by_key[key] = c
                by_overlap[ov] = key
            continue
        by_key[key] = c
        by_overlap[ov] = key

    return sorted(by_key.values(), key=lambda c: (-float(c.get("importance") or 0), str(c.get("id") or "")))


def _line_text(chunk):
    return _format_line(chunk, 0)


def _budget_for_lines(kept):
    body = "\n".join(_line_text(c) for c in kept)
    return _approx_tokens(body)


def _pick_within_budget(ranked, target_tokens, *, importance_threshold):
    must_keep = []
    flex = []
    for c in ranked:
        if float(c.get("importance") or 0) >= importance_threshold:
            must_keep.append(c)
        else:
            flex.append(c)
    must_keep_tokens = _budget_for_lines(must_keep)
    flex_budget = max(0, int(target_tokens) - must_keep_tokens - 50)
    kept = list(must_keep)
    dropped = []
    used = must_keep_tokens
    for c in flex:
        c_tokens = _approx_tokens(_line_text(c))
        if used + c_tokens <= flex_budget:
            kept.append(c)
            used += c_tokens
        else:
            dropped.append(c)
    return kept, dropped


def _find_existing_compressed(source_chunk_id, db_query):
    if not source_chunk_id:
        return None
    sql = (
        "SELECT id FROM memory_compressed "
        "WHERE source_chunk_id = %s "
        "ORDER BY created_at DESC LIMIT 1"
    )
    try:
        rows = db_query(sql, (source_chunk_id,), fetch="all")
    except Exception as exc:
        logger.warning("compact _find_existing_compressed failed: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        return row.get("id")
    if isinstance(row, (list, tuple)) and row:
        first = row[0]
        try:
            return int(first)
        except (TypeError, ValueError):
            return None
    return None


def _write_compressed_row(source_chunk_id, summary, importance, db_execute):
    sql = (
        "INSERT INTO memory_compressed "
        "(source_chunk_id, summary, importance, created_at) "
        "VALUES (%s, %s, %s, NOW()) "
        "RETURNING id"
    )
    try:
        result = db_execute(sql, (source_chunk_id, summary, float(importance)))
    except Exception as exc:
        logger.warning("compact _write_compressed_row failed: %s", exc)
        return None
    if isinstance(result, dict):
        return result.get("id")
    if isinstance(result, (list, tuple)):
        if not result:
            return None
        first = result[0]
        if isinstance(first, dict):
            return first.get("id")
        if isinstance(first, (list, tuple)) and first:
            try:
                return int(first[0])
            except (TypeError, ValueError):
                return first[0]
        try:
            return int(first)
        except (TypeError, ValueError):
            return first
    return None


def compact_long_conversation(
    chunks,
    target_tokens,
    *,
    summarize_fn=None,
    db_execute=None,
    db_query=None,
    template=None,
    importance_threshold=DEFAULT_IMPORTANCE_THRESHOLD,
    source_chunk_id=None,
    now=None,
):
    """Compact a long conversation into one summary row."""
    raw = list(chunks) if not isinstance(chunks, list) else chunks

    # Step 1: normalize.
    normalized = []
    skipped = []
    for c in raw:
        n = _normalize_chunk(c)
        if n is None or not n["content"]:
            if isinstance(c, dict):
                sid = str(c.get("id") or "")
                if sid:
                    skipped.append(sid)
            continue
        normalized.append(n)

    if not normalized:
        result = CompactionResult(
            summary="",
            summary_tokens=0,
            kept_chunk_ids=[],
            dropped_chunk_ids=[],
            skipped_chunk_ids=skipped,
            key_facts_preserved=[],
            source_chunk_id=str(source_chunk_id or ""),
            importance=0.5,
            persisted=False,
            persisted_skipped_reason="empty_input",
        )
        if now is not None:
            result.decided_at = now()
        return result

    # Step 2: dedupe.
    deduped = _dedupe_chunks(normalized)
    deduped_ids = set(c["id"] for c in deduped)
    dropped_in_dedupe = [c["id"] for c in normalized if c["id"] not in deduped_ids]

    # Step 3: budget cut.
    kept, flex_dropped = _pick_within_budget(
        deduped, int(target_tokens), importance_threshold=importance_threshold,
    )
    dropped_ids = dropped_in_dedupe + [c["id"] for c in flex_dropped]

    # Step 4: summarize.
    if int(target_tokens) <= 0 or not kept:
        summary_text = ""
    else:
        summary_text = summarize_chunks(
            kept, summarize_fn=summarize_fn, template=template,
        )

    resolved_source = source_chunk_id or (kept[0]["id"] if kept else "")
    if not resolved_source:
        user_id = ""
        for c in raw:
            if isinstance(c, dict):
                user_id = str(c.get("user_id") or "")
                if user_id:
                    break
        resolved_source = "compacted:" + (user_id or "anon")

    kept_importance = (
        max(float(c.get("importance") or 0) for c in kept) if kept else 0.5
    )

    key_facts = [
        c["id"] for c in kept
        if float(c.get("importance") or 0) >= importance_threshold
    ]

    summary_tokens = _approx_tokens(summary_text)

    # Step 5: persist with idempotency guard.
    persisted = False
    persisted_reason = ""
    compressed_id = None

    if not kept:
        persisted_reason = "empty_kept"
    elif db_execute is None:
        persisted_reason = "no_db_execute"
    else:
        if db_query is not None:
            existing = _find_existing_compressed(resolved_source, db_query)
            if existing is not None:
                persisted = True
                compressed_id = existing
                persisted_reason = "already_present"
        if not persisted:
            new_id = _write_compressed_row(
                resolved_source, summary_text, kept_importance, db_execute,
            )
            if new_id is not None:
                persisted = True
                compressed_id = new_id
            else:
                persisted_reason = "db_execute_failed"

    result = CompactionResult(
        summary=summary_text,
        summary_tokens=summary_tokens,
        kept_chunk_ids=[c["id"] for c in kept],
        dropped_chunk_ids=dropped_ids,
        skipped_chunk_ids=skipped,
        key_facts_preserved=key_facts,
        source_chunk_id=resolved_source,
        importance=kept_importance,
        persisted=persisted,
        persisted_skipped_reason=persisted_reason,
        compressed_id=compressed_id,
    )
    if now is not None:
        result.decided_at = now()
    return result


__all__ = [
    "CompactionResult",
    "DEFAULT_IMPORTANCE_THRESHOLD",
    "compact_long_conversation",
]
