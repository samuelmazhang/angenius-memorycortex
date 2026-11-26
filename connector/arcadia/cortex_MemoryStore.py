"""CortexMemoryStore — Cortex-only memory store wrapper.

Thin wrapper over ``tools.cortex_memory_backend`` that exposes the same
``add / replace / remove / recall / deep_context / apply_batch`` shape that
``MemoryStore`` (tools/memory_tool.py) provides, but routes every read and
write through the remote Memory Cortex service (127.0.0.1:1880 by default
via ``MEMORY_CORTEX_URL`` / ``CORTEX_TAKEOVER=1`` env).

Why a separate class instead of reusing MemoryStore(use_cortex=True)?

1. **Hard separation of concerns** — code paths that *must* talk to Cortex
   (e.g. the memory-cortex plugin prefetch, cortex_search tool, khop
   reasoning) can import this module without dragging in the .md-file
   fallback logic that MemoryStore carries for ``CORTEX_TAKEOVER=0`` dev
   sessions.
2. **No .md files ever touched** — even when cortex returns an error,
   the wrapper returns a JSON error string rather than silently
   degrading to disk. (MemoryStore(use_cortex=True) silently falls
   back to .md when cortex raises CortexUnavailableError; that's the
   right default for the built-in ``memory`` tool but wrong for code
   paths that promise callers a Cortex view.)
3. **D-style honest** — this module was created on 2026-09-11 per user
   OOB 2026-09-11 "在源代码目录里创建复核系统要求的 cortex_MemoryStore
   文件". It is a *reasonable* wrapper design inferred from the existing
   MemoryStore + cortex_memory_backend public surface; if the 复核系统
   requires a different shape (specific class name, methods, return
   schema), edit this file to match — don't fork it.

Public surface (mirrors MemoryStore for parity where it makes sense,
plus a few Cortex-only operations that are awkward to express through
MemoryStore's generic .md abstraction):

    store = CortexMemoryStore()
    store.add(target="memory", content="...", importance=7, tags=[...])
    store.remove(target="memory", old_text="...")
    store.replace(target="memory", old_text="...", content="...")
    store.recall(query="...", limit=5)
    store.deep_context(chunk_id="mem_...")
    store.apply_batch(target="memory", operations=[...])
    store.health()
    store.session_summary(content=..., session_id=...)
    store.search(query=..., limit=..., include_related=True)
    store.reason_with_memory(query=..., top_k=..., khop=...)

Backwards-compat:
    CortexMemoryStore.entry_count(target="memory") -> int  (best-effort)
    CortexMemoryStore.load_from_disk() -> None  (no-op; cortex is always live)

Errors: ``CortexUnavailableError`` propagates from the backend. Callers
that want graceful degradation should wrap calls in try/except.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

from tools import cortex_memory_backend as _cmb
from tools.cortex_memory_backend import CortexUnavailableError

logger = logging.getLogger(__name__)

__all__ = ["CortexMemoryStore", "CortexUnavailableError"]


# Mirrors MemoryStore's tier→importance defaults; copied (not imported)
# because MemoryStore's mapping is private and may evolve.
_TIER_TO_IMPORTANCE = {
    "short": 3,
    "medium": 5,
    "long": 7,
    "permanent": 9,
}


def _coerce_importance(tier: Optional[str], importance: Optional[int]) -> int:
    """Resolve importance from explicit arg or tier mapping (matches
    MemoryStore conventions so callers can use either spelling)."""
    if importance is not None:
        return importance
    if tier and tier in _TIER_TO_IMPORTANCE:
        return _TIER_TO_IMPORTANCE[tier]
    return 7  # MemoryStore default (long_term)


def _normalize_target(target: str) -> str:
    """Accept the same target spellings MemoryStore does: ``memory`` /
    ``user`` / legacy alias ``profile``. Cortex itself only knows
    ``memory`` / ``user``; pass through anything else untouched so the
    backend raises a clear error."""
    if target == "profile":
        return "user"
    return target


class CortexMemoryStore:
    """Cortex-only MemoryStore wrapper.

    Mirrors ``MemoryStore``'s public shape (``add / replace / remove /
    recall / deep_context / apply_batch``) but every call is a thin
    adapter onto ``tools.cortex_memory_backend`` — never falls back to
    a local file. Use this when the caller has a Cortex-only promise;
    use ``MemoryStore(use_cortex=True)`` when graceful degradation to
    a .md file is desired.
    """

    def __init__(
        self,
        *,
        backend: Optional[Any] = None,
        char_limit_memory: int = 220000,
        char_limit_user: int = 137500,
    ) -> None:
        """Initialize the wrapper.

        ``backend`` (optional pre-built ``cortex_memory_backend``
        module shim) is accepted for tests / dependency injection.
        Defaults to the live `` ``tools.cortex_memory_backend`` import.

        ``char_limit_memory`` / ``char_limit_user`` are kept for API
        parity with ``MemoryStore.__init__`` and surfaced via
        ``entry_count`` for callers that want to reason about budget
        pressure; the values themselves are advisory because cortex
        has its own tier-based retention rather than a single
        ``char_limit`` cap per store.
        """
        self._backend = backend if backend is not None else _cmb
        self._char_limit_memory = char_limit_memory
        self._char_limit_user = char_limit_user
        # Mirror MemoryStore's chunk-id map (target → {content → chunk_id}).
        # Populated by ``load_from_disk`` and refreshed after add/replace/remove.
        # Used to map ``old_text`` substring → chunk_id for replace/remove.
        self._chunk_ids: Dict[str, Dict[str, str]] = {"memory": {}, "user": {}}

    # ------------------------------------------------------------------
    # MemoryStore-shaped parity API
    # ------------------------------------------------------------------

    def add(
        self,
        target: str,
        content: str,
        *,
        importance: Optional[int] = None,
        tier: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add one entry. Returns the cortex chunk dict (``id``,
        ``tier``, ``importance``, etc).

        Wraps ``cortex_memory_backend.create_entry`` and refreshes the
        chunk-id cache for the (target, content) pair.
        """
        target = _normalize_target(target)
        if not content:
            return {"success": False, "error": "content is empty"}
        imp = _coerce_importance(tier, importance)
        result = self._backend.create_entry(
            target=target,
            content=content,
            importance=imp,
            tags=list(tags) if tags else None,
            source_context=source_context,
        )
        # Refresh chunk-id cache when the backend returns one.
        if isinstance(result, dict) and result.get("id"):
            self._chunk_ids.setdefault(target, {})[content] = result["id"]
        return result

    def replace(
        self,
        target: str,
        old_text: str,
        content: str,
        *,
        importance: Optional[int] = None,
        tier: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Replace the entry whose content contains ``old_text`` with
        ``content``. Returns ``{"success": True, "deleted": chunk_id,
        "added": new_chunk_dict}`` on success.

        Cortex has no native "update" in-place operation: we resolve
        ``old_text`` to the matching chunk via the chunk-id cache (or a
        cortex search as a fallback), DELETE the old chunk, then
        create a new one. This matches MemoryStore's replace semantics
        even though the underlying primitive is two HTTP round-trips.
        """
        target = _normalize_target(target)
        if not old_text:
            return {"success": False, "error": "old_text is required"}
        if not content:
            return {"success": False, "error": "content is empty"}
        chunk_id = self._resolve_chunk_id(target, old_text)
        if not chunk_id:
            return {
                "success": False,
                "error": f"No chunk matches old_text substring: {old_text!r}",
            }
        try:
            self._backend.delete_entry(chunk_id)
        except CortexUnavailableError as exc:
            return {"success": False, "error": f"cortex delete failed: {exc}"}
        # Invalidate the chunk-id cache for the old content.
        for cached_content, cached_id in list(self._chunk_ids.get(target, {}).items()):
            if cached_id == chunk_id:
                self._chunk_ids[target].pop(cached_content, None)
        added = self.add(
            target=target,
            content=content,
            importance=importance,
            tier=tier,
            tags=tags,
        )
        return {"success": True, "deleted": chunk_id, "added": added}

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Delete the entry whose content contains ``old_text``."""
        target = _normalize_target(target)
        if not old_text:
            return {"success": False, "error": "old_text is required"}
        chunk_id = self._resolve_chunk_id(target, old_text)
        if not chunk_id:
            return {
                "success": False,
                "error": f"No chunk matches old_text substring: {old_text!r}",
            }
        try:
            deleted = self._backend.delete_entry(chunk_id)
        except CortexUnavailableError as exc:
            return {"success": False, "error": f"cortex delete failed: {exc}"}
        # Invalidate cache.
        for cached_content, cached_id in list(self._chunk_ids.get(target, {}).items()):
            if cached_id == chunk_id:
                self._chunk_ids[target].pop(cached_content, None)
        return {"success": bool(deleted), "deleted": chunk_id}

    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        include_related: bool = True,
    ) -> Dict[str, Any]:
        """Agentic recall: hybrid semantic + lexical + tag retrieval.

        Thin wrapper over ``cortex_memory_backend.search``; returns
        the raw search response so callers can inspect
        ``composite_score`` / ``rrf_score`` / ``relevance_score``.
        """
        return self._backend.search(
            query,
            limit=int(limit),
            include_related=bool(include_related),
        )

    def deep_context(self, chunk_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Return the full provenance / history / relations / hierarchy
        / links for ``chunk_id``. Thin wrapper over
        ``cortex_memory_backend.deep_context_for``."""
        return self._backend.deep_context_for(chunk_id, **kwargs)

    def apply_batch(
        self,
        target: str,
        operations: Iterable[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Apply a sequence of add / replace / remove ops to one
        target. Atomic-ish (cortex is HTTP; we run ops sequentially
        and report the first failure). Mirrors MemoryStore.apply_batch's
        shape: ``{"success": True, "applied": [...]}`` or
        ``{"success": False, "error": "...", "failed_at": <index>}``.
        """
        target = _normalize_target(target)
        ops = list(operations)
        if not ops:
            return {"success": False, "error": "operations list is empty"}
        applied: List[Dict[str, Any]] = []
        for idx, op in enumerate(ops):
            action = (op or {}).get("action")
            try:
                if action == "add":
                    res = self.add(
                        target=target,
                        content=op.get("content", ""),
                        importance=op.get("importance"),
                        tier=op.get("tier"),
                        tags=op.get("tags"),
                    )
                elif action == "replace":
                    res = self.replace(
                        target=target,
                        old_text=op.get("old_text", ""),
                        content=op.get("content", ""),
                        importance=op.get("importance"),
                        tier=op.get("tier"),
                        tags=op.get("tags"),
                    )
                elif action == "remove":
                    res = self.remove(target=target, old_text=op.get("old_text", ""))
                else:
                    return {
                        "success": False,
                        "error": f"Operation {idx + 1}: unknown action {action!r}",
                        "failed_at": idx,
                    }
            except CortexUnavailableError as exc:
                return {
                    "success": False,
                    "error": f"Operation {idx + 1}: cortex unavailable: {exc}",
                    "failed_at": idx,
                    "applied": applied,
                }
            if isinstance(res, dict) and res.get("success") is False:
                return {
                    "success": False,
                    "error": res.get("error", "unknown failure"),
                    "failed_at": idx,
                    "applied": applied,
                }
            applied.append(res)
        return {"success": True, "applied": applied, "count": len(applied)}

    # ------------------------------------------------------------------
    # MemoryStore-shaped parity helpers
    # ------------------------------------------------------------------

    def entry_count(self, target: str) -> int:
        """Approximate entry count for budget reporting. Calls
        ``cortex_memory_backend.stats`` (the live dashboard endpoint)
        and reads the chunk count; falls back to the cached chunk-id
        map on cortex failure.
        """
        target = _normalize_target(target)
        try:
            dash = self._backend.stats()
            if isinstance(dash, dict):
                # stats() returns the dashboard JSON: total_memories /
                # chunks / vectors etc. Per-target split isn't exposed,
                # so callers should treat this as approximate.
                return int(
                    dash.get("total_memories")
                    or dash.get("chunks")
                    or dash.get("total_chunks")
                    or 0
                )
        except CortexUnavailableError:
            pass
        return len(self._chunk_ids.get(target, {}))

    def char_count(self, target: str) -> int:
        """Approximate char count for budget reporting. Cortex doesn't
        expose a per-target char total, so this returns ``0`` and the
        caller should not rely on it for budget enforcement (cortex
        uses tier-based retention, not a single char cap)."""
        return 0

    def char_limit(self, target: str) -> int:
        """Return the configured char limit for ``target`` (mirrors
        MemoryStore.char_limit). Advisory only — cortex has its own
        retention model."""
        if target == "user":
            return self._char_limit_user
        return self._char_limit_memory

    def load_from_disk(self) -> None:
        """No-op for parity with MemoryStore.load_from_disk. The chunk-id
        cache is left empty; ``resolve_chunk_id`` falls back to a
        cortex search when the cache misses, so the first add/replace/
        remove on each entry will lazily populate the cache.
        """
        # Best-effort cache prime for the small working set we know
        # about. Callers that want a hot cache should call
        # ``prime_cache(target=...)`` explicitly.
        return None

    def prime_cache(self, target: str, *, limit: int = 200) -> int:
        """Populate the chunk-id cache by listing recent entries for
        ``target`` (uses ``cortex_memory_backend.list`` /
        ``search("")`` as the listing primitive). Returns the count
        of entries primed.
        """
        target = _normalize_target(target)
        try:
            # ``search`` with an empty query returns the most-recent
            # chunks sorted by recency — a reasonable proxy for "list".
            result = self._backend.search("", limit=int(limit))
            hits = result.get("results") if isinstance(result, dict) else []
            self._chunk_ids.setdefault(target, {})
            for hit in hits:
                cid = hit.get("id")
                content = hit.get("content") or hit.get("preview") or ""
                if cid and content:
                    self._chunk_ids[target][content] = cid
            return len(hits)
        except CortexUnavailableError as exc:
            logger.debug("prime_cache: cortex unavailable: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Cortex-only operations (no MemoryStore equivalent)
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Cortex liveness + version + counts. Thin wrapper over
        ``cortex_memory_backend.agent_health_check``."""
        return self._backend.agent_health_check()

    def search(self, query: str, *, limit: int = 5, include_related: bool = True) -> Dict[str, Any]:
        """Alias for ``recall`` kept for code-paths that already import
        ``.search`` from the cortex backend module."""
        return self.recall(query, limit=limit, include_related=include_related)

    def reason_with_memory(
        self,
        query: str,
        *,
        top_k: int = 3,
        include_hierarchy: bool = True,
        khop: int = 2,
    ) -> Dict[str, Any]:
        """Multi-hop graph-aware reasoning over memory chunks. Thin
        wrapper over ``cortex_memory_backend.reason_with_memory``."""
        khop = max(1, min(int(khop), 3))  # cap at 3 per memory-cortex-client skill §"Exposing advanced Cortex features"
        return self._backend.reason_with_memory(
            query,
            top_k=int(top_k),
            include_hierarchy=bool(include_hierarchy),
            khop=khop,
        )

    def session_summary(
        self,
        content: str,
        *,
        session_id: str = "",
        importance: int = 7,
        tags: Optional[List[str]] = None,
        source: str = "cortex_MemoryStore",
    ) -> Dict[str, Any]:
        """Persist a session-level summary to cortex. Thin wrapper over
        ``cortex_memory_backend.create_session_summary``."""
        return self._backend.create_session_summary(
            content,
            importance=int(importance),
            tags=list(tags) if tags else None,
            session_id=session_id or "",
            source=source,
        )

    def is_available(self) -> bool:
        """Whether cortex is reachable. Mirrors the
        ``CortexMemoryProvider.is_available`` shape used by the
        memory-cortex plugin."""
        try:
            return bool(self._backend.is_cortex_active())
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("is_available probe failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_chunk_id(self, target: str, old_text: str) -> Optional[str]:
        """Map an ``old_text`` substring to a cortex chunk_id.

        Tries (in order):
          1. Exact cache match on (target, content).
          2. Substring search of cache contents.
          3. Cortex search fallback (returns the most-recent hit
             whose content contains ``old_text``).
        """
        cache = self._chunk_ids.get(target, {})
        if old_text in cache:
            return cache[old_text]
        for cached_content, chunk_id in cache.items():
            if old_text in cached_content:
                return chunk_id
        # Fall back to cortex search.
        try:
            result = self._backend.search(old_text, limit=3, include_related=False)
        except CortexUnavailableError as exc:
            logger.debug("resolve_chunk_id: cortex unavailable: %s", exc)
            return None
        hits = result.get("results") if isinstance(result, dict) else []
        for hit in hits:
            content = hit.get("content") or ""
            if old_text in content and hit.get("id"):
                # Cache the match for next time.
                cache[content] = hit["id"]
                return hit["id"]
        return None