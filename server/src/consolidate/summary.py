"""
Cortex v6.5.0 -- Consolidation Pipeline (#C): chunk-level summarizer.

Purpose
-------
Turn a list of memory chunks (already pruned for redundancy by
``compact_long_conversation``) into a single coherent summary string.

The summarizer is intentionally LLM-friendly while remaining LLM-free
in its default mode. Production can wire in any synchronous
``summarize_fn(texts, ...)`` callable that returns a ``str``; the
default implementation is deterministic (joins content in importance
order with a fixed template) and fully unit-testable without network
access.

Design constraints (from dispatch prompt):
  * No LLM in this file's defaults.
  * ``summarize_fn`` is injected; production may pass a thin wrapper
    around an LLM endpoint or any callable returning a string.
  * The output is a ``str``. Token estimation uses a heuristic
    (``len(text) / CHARS_PER_TOKEN``) which is the same heuristic
    used by ``compact.py`` for budget math -- this keeps the two
    modules on the same scale.
  * Stable ordering + stable templates guarantee idempotency of
    ``compact_long_conversation`` even with the default stub.

summarize_fn contract (v6.5.1+, ADR-XXX):
----------------------------------------
Unified across the consolidation pipeline: ``summarize_fn(texts: list[str]) -> str``.

This matches ``raptor.build_tree`` (which always passed ``list[str]``
to the injected fn). Before this ADR the contracts DIVERGED:

  * consolidate.summary.summarize_chunks -> passed list[Dict]
    ({"id", "content", "importance"})
  * raptor.build_tree                  -> passed list[str]

This forced every operator who wired one function for both call
sites to write a thin shim. The contract is now unified on
``list[str]``. For callers who still have a dict-style function
written against the old contract, ``adapt_dict_summarize_fn`` wraps
it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional


logger = logging.getLogger("cortex.consolidate.summary")


# Chars-per-token heuristic. Same as ``compact._approx_tokens`` so the
# summary's estimated tokens line up with the budget.
CHARS_PER_TOKEN = 4

# Default fallback template (used when ``summarize_fn`` is None and
# ``template`` is not overridden). One chunk per line, importance first
# so the highest-signal facts lead.
DEFAULT_TEMPLATE = (
    "Summary of {count} chunk(s) (top-importance first):\n"
    "{body}\n"
    "End of summary."
)


# ---------- Type alias for the unified contract ----------

# A summarize_fn receives normalized texts (one string per chunk) and
# returns a single summary string. This is the SAME signature that
# raptor.build_tree uses; consolidating here removes the v6.5.0
# contract divergence (ADR-XXX).
SummarizeFn = Callable[[List[str]], str]


# ---------- Helpers (pure functions -- fully unit-testable) ----------


def _approx_tokens(text: str) -> int:
    """Rough token estimate: ceil(len / CHARS_PER_TOKEN).

    Strips whitespace on each edge before measuring so empty lines do
    not inflate the budget. Always returns a non-negative integer.
    """
    if not text:
        return 0
    n = len(text.strip())
    if n == 0:
        return 0
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _normalize_chunk(chunk: Any) -> Optional[Dict[str, Any]]:
    """Coerce a chunk-like input into ``{id, content, importance}``.

    Whitespace-only content is treated as empty: ``"   "`` becomes
    ``""`` so the caller (notably ``compact_long_conversation``) can
    distinguish effectively-empty chunks from real content and route
    them to ``skipped_chunk_ids``.
    """
    if not isinstance(chunk, dict):
        return None
    cid = chunk.get("id") or chunk.get("chunk_id") or ""
    raw_content = chunk.get("content") or chunk.get("text") or ""
    content = str(raw_content).strip()
    imp = chunk.get("importance")
    try:
        importance = float(imp) if imp is not None else 5.0
    except (TypeError, ValueError):
        importance = 5.0
    return {"id": str(cid), "content": content, "importance": importance}


def _format_line(item: Dict[str, Any], idx: int) -> str:
    """Render one chunk as a summary line."""
    cid = item["id"] or f"#{idx}"
    importance = item["importance"]
    content = item["content"].strip()
    return f"[{idx}] (imp={importance:.1f}, id={cid}) {content}"


def adapt_dict_summarize_fn(dict_fn: Callable[[List[Dict[str, Any]]], str]) -> SummarizeFn:
    """Adapter for callers who still have a v6.5.0 dict-style
    summarize_fn (the contract that pre-dated the ADR-XXX unification).

    The adapter reconstructs the ``{"id", "content", "importance"}``
    dicts from the texts list -- using a parallel index sequence --
    and hands them to ``dict_fn``. Id/importance are unknown at the
    adapter level (we only have texts here), so they default to
    ``""`` and ``5.0`` respectively; callers who need real values
    should re-implement summarize_fn against the unified ``list[str]``
    contract.
    """
    def adapter(texts: List[str]) -> str:
        dicts = [
            {"id": "", "content": t, "importance": 5.0}
            for t in texts
        ]
        return dict_fn(dicts)
    return adapter


# ---------- Public entry ----------


def summarize_chunks(
    chunks: Optional[Iterable[Any]],
    *,
    summarize_fn: Optional[SummarizeFn] = None,
    template: Optional[str] = None,
) -> str:
    """Summarize an iterable of chunks into a single ``str``.

    See module docstring for semantics. Returns ``""`` for empty input
    (including ``None``). Any exception raised by a caller-supplied
    ``summarize_fn`` is caught, logged, and the deterministic template
    is used as fallback.

    .. note::

        ``summarize_fn`` receives ``list[str]`` (one entry per chunk,
        in the same importance-desc / id-asc order as the default
        fallback). Use :func:`adapt_dict_summarize_fn` if you have a
        legacy dict-style function from before v6.5.1.
    """
    normalized: List[Dict[str, Any]] = []
    if chunks is not None:
        for c in chunks:
            n = _normalize_chunk(c)
            if n is not None and n["content"]:
                normalized.append(n)

    if not normalized:
        return ""

    # Stable importance-desc, then stable id-asc tiebreak -- done once
    # so the unified contract and the default fallback see the same
    # ordering.
    sorted_chunks = sorted(
        normalized,
        key=lambda c: (-c["importance"], c["id"]),
    )

    if summarize_fn is not None:
        try:
            # Unified contract: pass list[str].
            texts = [c["content"] for c in sorted_chunks]
            out = summarize_fn(texts)
        except Exception as exc:  # noqa: BLE001 -- caller fn must not crash us
            logger.warning(
                "summarize_chunks summarize_fn raised, falling back: %s",
                exc,
            )
            out = None
        if isinstance(out, str) and out:
            return out

    # Default: deterministic template over the same sorted_chunks.
    body = "\n".join(_format_line(c, i + 1) for i, c in enumerate(sorted_chunks))
    tmpl = template or DEFAULT_TEMPLATE
    try:
        return tmpl.format(count=len(sorted_chunks), body=body)
    except (KeyError, IndexError) as exc:
        logger.warning(
            "summarize_chunks template substitution failed: %s; using plain body",
            exc,
        )
        return body


__all__ = [
    "summarize_chunks",
    "adapt_dict_summarize_fn",
    "SummarizeFn",
    "CHARS_PER_TOKEN",
    "DEFAULT_TEMPLATE",
]
