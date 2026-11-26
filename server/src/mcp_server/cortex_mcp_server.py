"""Memory Cortex as an MCP server (streamable-http), mounted into the
Cortex FastAPI app at ``/mcp``.

This module owns three things:

1. The FastMCP instance with the 12 curated tools registered.
2. An ASGI bridge that handles FastMCP's session-manager lifecycle
   (which ``streamable_http_app()`` doesn't expose when embedded) and
   normalizes the path that Starlette's ``Mount`` passes through.
3. A lifespan context manager that the parent ``api_pg.py`` plugs in
   so FastMCP's anyio task group is properly initialized at app
   startup and torn down on shutdown.

Why not just ``app.mount("/mcp", mcp.streamable_http_app())``?
=================================================================

Two failure modes meet there:

* Starlette's ``Mount`` sets ``scope["root_path"] = "/mcp"`` while
  leaving ``scope["path"] = "/mcp"`` (or ``"/mcp/"`` after the trailing-
  slash redirect). Starlette's ``get_route_path`` helper strips
  ``root_path`` from ``path``; with both equal, the helper returns
  ``""`` and the sub-app's routing never matches.

* FastMCP's session manager is initialized lazily inside
  ``streamable_http_app()``. When that sub-app is mounted into a
  parent FastAPI app, the parent's lifespan doesn't start the
  FastMCP session manager's anyio task group, so the first tool call
  raises ``RuntimeError: Task group is not initialized``.

The bridge below handles both: it clears ``root_path`` and strips
trailing slashes before forwarding, and the parent FastAPI app calls
``get_mcp_lifespan()`` at startup so the session manager is ready.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

# fastmcp 3.x moved FastMCP out of mcp.server.fastmcp; use the standalone package.
from fastmcp import FastMCP
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# Resolve the repo root once. From the container's bind-mount
# (``/app/src/mcp_server/cortex_mcp_server.py``) we expect:
#   _THIS_DIR   = /app/src/mcp_server
#   _REPO_ROOT  = /app/src     (parent.parent of _THIS_DIR)
#   _SDK_DIR    = /app/src/sdk (MemoryCortexClient lives here)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))   # /app/src/mcp_server
_REPO_ROOT = os.path.dirname(_THIS_DIR)                   # /app/src
_SDK_DIR = os.path.join(_REPO_ROOT, "sdk")                # /app/src/sdk
for _p in (_SDK_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cortex_client  # noqa: E402

logger = logging.getLogger(__name__)


def _coerce_tags(value, *, default=None):
    """Accept tags as either a list[str] or a comma/whitespace-separated string.

    FastMCP 3.x cannot derive an ``array`` JSON Schema from bare ``list`` type
    hints, so the schema we expose over MCP ends up as ``{"type": "string"}``.
    Real-world clients (and our own composite ``memory`` tool) sometimes pass
    ``"a,b,c"`` while others pass ``["a","b","c"]``. Coerce everything to a
    clean ``list[str]`` (or ``default``) so the downstream ``MemoryChunkCreate``
    Pydantic validator — which mandates ``List[str]`` — does not 422.
    """
    if value is None:
        return list(default) if default is not None else []
    if isinstance(value, str):
        return [t.strip() for t in value.replace("\n", ",").split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        out = []
        for t in value:
            if isinstance(t, str):
                t = t.strip()
                if t:
                    out.append(t)
            elif t is not None:
                out.append(str(t))
        return out
    # last-ditch fallback: stringify
    return [str(value)]


_CORTEX_CLIENT = cortex_client.MemoryCortexClient("http://127.0.0.1:1880")


def _tool_remember(content: str, type: str = "knowledge", tags: list[str] | str | None = None,
                   importance: int = 5, user_id: str | None = None) -> dict:
    """Store a memory chunk. Returns the new chunk_id.

    If ``user_id`` is provided, it is persisted into ``chunk.metadata.user_id``
    so that future ``cortex_recall(user_id=...)`` calls can filter to this
    user's namespace and the conflict-resolve bucket (keyed on
    ``metadata->>'user_id'``) actually fires.
    Backward-compat: ``user_id=None`` -> legacy shared pool (no metadata.user_id).

    ``tags`` accepts either a list[str] (preferred — array JSON Schema) or a
    comma-separated string (legacy — string JSON Schema). Both go through
    ``_coerce_tags`` so the downstream Pydantic ``MemoryChunkCreate.tags``
    (``List[str]``) never sees the wrong shape.
    """
    tags = _coerce_tags(tags)
    return _CORTEX_CLIENT.remember(
        content=content, type=type, tags=tags,
        importance=int(importance),
        user_id=user_id,
    )


def _tool_recall(query: str, limit: int = 5, min_importance: int = 1,
                 user_id: str | None = None) -> dict:
    """Hybrid semantic search; returns ranked memory chunks.

    If ``user_id`` is provided, only chunks whose ``metadata.user_id`` matches
    are returned (client-side filter; backend /search is not user_id-aware).

    Note: SDK's ``recall()`` does not currently accept ``min_importance``;
    we over-fetch (``limit + N``) and post-filter to approximate the bound.
    """
    over_fetch = max(int(limit) * 4, int(limit) + 20)
    hits = _CORTEX_CLIENT.recall(
        query=query, top_k=over_fetch,
        user_id=user_id,
    )
    # hits may be {'results': [...]} or a bare list
    if isinstance(hits, dict) and 'results' in hits:
        results = hits['results']
    elif isinstance(hits, list):
        results = hits
    else:
        results = []
    filtered = [
        r for r in results
        if int(r.get('importance', 0)) >= int(min_importance)
    ][:int(limit)]
    return {'total': len(filtered), 'results': filtered}


def _tool_forget(chunk_id: str) -> dict:
    """Permanently delete a memory chunk by id."""
    return _CORTEX_CLIENT.forget(chunk_id=chunk_id)


def _tool_introspect() -> dict:
    """Cortex self-aware metadata: health, version, totals."""
    return _CORTEX_CLIENT.introspect()


def _tool_conflict_upsert(content: str, source_key: str, importance: int = 7,
                          confidence: float = 0.8, type: str = "fact",
                          tags: list[str] | str | None = None,
                          auto_resolve: bool = True,
                          user_id: str | None = None) -> dict:
    """Insert a chunk, or supersede the prior one for this ``source_key``.

    V5.4 conflict-resolve: calls the real ``/conflict/upsert`` HTTP endpoint
    so SUPERSEDE actually deprecates the prior chunk for this ``source_key``
    and writes a row to the ``conflict_log`` audit table.

    Returns a flat dict::

        {"chunk_id": str, "action": str, "old_chunk_id": str|None, "reason": str}

    where ``action ∈ {"noop","supersede","mark_only","error"}``.

    Note: bucket key is ``(metadata.user_id, source_key)``. With
    ``user_id=None`` the V5.4 SQL lookup compares against empty string and
    will never match, so SUPERSEDE never fires. Pass a non-empty
    ``user_id`` (e.g. ``"<user_id>"``) to get real
    conflict-resolve behavior.

    History: 2026-08-20 — replaced the previous ``remember()``-based stub
    (which encoded ``source_key``/``confidence`` as tags but did NOT trigger
    real V5.4 conflict resolution). The old behavior silently produced
    duplicate chunks for the same ``source_key``.
    """
    body = {
        "content": content,
        "source_key": source_key,
        "importance": int(importance),
        "confidence": float(confidence),
        "type": type,
        "tags": _coerce_tags(tags, default=[]),
        "auto_resolve": bool(auto_resolve),
    }
    if user_id:
        body["user_id"] = user_id
    resp = _CORTEX_CLIENT._request("POST", "/conflict/upsert", body=body) or {}
    decision = resp.get("decision") or {}
    return {
        "chunk_id": resp.get("chunk_id"),
        "action": decision.get("action"),
        "old_chunk_id": decision.get("old_chunk_id"),
        "reason": decision.get("reason"),
    }


def _tool_get(memory_id: str) -> dict:
    """Fetch a single memory chunk with full metadata + history."""
    return _CORTEX_CLIENT.get_chunk(chunk_id=memory_id)


TOOLS: list[tuple[str, str, Callable[..., Any], dict]] = [
    ("cortex_remember", "Store a memory chunk. Returns the new chunk_id. "
                        "Optional user_id persists into chunk.metadata.user_id.",
     _tool_remember,
     {"content": str, "type": str, "tags": list, "importance": int, "user_id": str}),
    ("cortex_recall", "Hybrid semantic search over memory chunks. "
                      "Optional user_id filters results to that user's namespace.",
     _tool_recall,
     {"query": str, "limit": int, "min_importance": int, "user_id": str}),
    ("cortex_forget", "Permanently delete a memory chunk by id.",
     _tool_forget,
     {"chunk_id": str}),
    ("cortex_introspect", "Cortex self-aware metadata: health, version, totals.",
     _tool_introspect, {}),
    ("cortex_conflict_upsert",
     "Insert a chunk, or supersede the prior one for this source_key.",
     _tool_conflict_upsert,
     {"content": str, "source_key": str, "importance": int, "confidence": float,
      "type": str, "tags": list, "auto_resolve": bool, "user_id": str}),
    ("cortex_get", "Fetch one memory chunk with full metadata + history.",
     _tool_get, {"memory_id": str}),
]


# ============================================================================
# Extended tools (V6.3.1+, full endpoint surface — 98 additional tools)
#
# Naming convention: cortex_<group>_<action>, mirroring cortex's OpenAPI
# operationId. Each handler is a thin wrapper around either a
# MemoryCortexClient method (when one exists) or _CORTEX_CLIENT._request()
# for endpoints the SDK does not yet wrap.
#
# Backward-compat: the 6 friendly tools above (cortex_remember, cortex_recall,
# cortex_forget, cortex_conflict_upsert, cortex_get) are kept as aliases for
# the most common operations. The technical names below cover the full
# surface (admin, graph, relations, working, forgetting, audit, hierarchy,
# procedural, episodes, media, resources, conflict, rumination, maintenance,
# compress, consolidate, eval/bench, taxonomy, storage, etc.).
# ============================================================================


# ---------- System / health / stats ----------

def _tool_health() -> dict:
    """Service health check (version, status, counts)."""
    return _CORTEX_CLIENT._request("GET", "/health")


def _tool_stats() -> dict:
    """Overall stats: total, tier breakdown, by-source."""
    return _CORTEX_CLIENT._request("GET", "/stats")


def _tool_stats_by_type() -> dict:
    """Stats grouped by chunk type (knowledge/fact/preference/...)."""
    return _CORTEX_CLIENT._request("GET", "/stats/by-type")


def _tool_stats_by_source() -> dict:
    """Stats grouped by source label (user/memory_tool/hermes-session-summarizer/...)."""
    return _CORTEX_CLIENT._request("GET", "/stats/by-source")


def _tool_stats_by_status() -> dict:
    """V6.2 explicit 3-bucket status breakdown (active+deprecated+archived)."""
    return _CORTEX_CLIENT._request("GET", "/stats/by-status")


def _tool_stats_by_deprecated() -> dict:
    """Stats by is_deprecated flag (governance view)."""
    return _CORTEX_CLIENT._request("GET", "/stats/by-deprecated")


def _tool_stats_by_importance() -> dict:
    """Stats grouped by importance bucket (0-2/3-5/6-8/9-10)."""
    return _CORTEX_CLIENT._request("GET", "/stats/by-importance")


def _tool_stats_ops() -> dict:
    """Operational counters (writes/reads/conflicts/etc)."""
    return _CORTEX_CLIENT._request("GET", "/stats/ops")


def _tool_stats_embed_cache() -> dict:
    """Embedding cache hit/miss stats (V6.2)."""
    return _CORTEX_CLIENT._request("GET", "/stats/embed-cache")


def _tool_stats_health() -> dict:
    """Detailed health: DB + vector index + cache + counters (V6.2)."""
    return _CORTEX_CLIENT._request("GET", "/stats/health")


def _tool_stats_ops_reset() -> dict:
    """Reset ops counters to zero."""
    return _CORTEX_CLIENT._request("POST", "/stats/ops/reset")


def _tool_mode() -> dict:
    """Current cortex runtime mode (live/test)."""
    return _CORTEX_CLIENT._request("GET", "/mode")


def _tool_endpoints_list() -> dict:
    """List every registered endpoint with metadata."""
    return _CORTEX_CLIENT._request("GET", "/endpoints")


def _tool_endpoints_recommend(scenario: str) -> dict:
    """Recommend endpoints for a scenario keyword (e.g. 'graph', 'forgetting')."""
    return _CORTEX_CLIENT._request("GET", f"/endpoints/recommend/{scenario}")


def _tool_dashboard() -> dict:
    """Dashboard JSON: stats + 10 most recent chunks."""
    return _CORTEX_CLIENT._request("GET", "/dashboard")


def _tool_dashboard_html() -> str:
    """Dashboard HTML page (4 tabs: overview/stats/search/graph)."""
    return _CORTEX_CLIENT._request("GET", "/dashboard/html")


# ---------- Memory chunks (CRUD, beyond remember/forget/get) ----------

def _tool_chunks_create_batch(chunks: list) -> dict:
    """Bulk create up to 500 chunks. Returns {created, ids, errors}."""
    return _CORTEX_CLIENT._request("POST", "/chunks/batch", body={"chunks": chunks})


def _tool_chunks_update(chunk_id: str, content: str, type: str = "knowledge",
                        tags: list[str] | str | None = None, source: str = "user",
                        importance: int = 5) -> dict:
    """Full-replace a chunk (PUT-style). Re-derives embedding + tier/TTL."""
    return _CORTEX_CLIENT.update_chunk(
        chunk_id=chunk_id, content=content, type=type,
        tags=_coerce_tags(tags, default=[]), source=source, importance=int(importance),
    )


def _tool_chunks_patch(chunk_id: str, content: str | None = None,
                       type: str | None = None, tags: list[str] | str | None = None,
                       source: str | None = None, importance: int | None = None) -> dict:
    """Partial-update a chunk. Only non-None fields are applied. PATCH-style."""
    body: dict = {}
    if content is not None: body["content"] = content
    if type is not None: body["type"] = type
    if tags is not None: body["tags"] = _coerce_tags(tags)
    if source is not None: body["source"] = source
    if importance is not None: body["importance"] = importance
    return _CORTEX_CLIENT._request("PATCH", f"/chunks/{chunk_id}", body=body)


def _tool_chunks_list(type: str | None = None, tag: str | None = None,
                      limit: int = 50, offset: int = 0) -> dict:
    """List chunks with filters (type, tag, limit, offset)."""
    return _CORTEX_CLIENT.list_chunks(type=type, tags=[tag] if tag else None, limit=int(limit))


def _tool_admin_deprecate_by_type(type: str, dry_run: bool = True) -> dict:
    """Admin: bulk deprecate all chunks of a type. Set dry_run=true to preview."""
    return _CORTEX_CLIENT._request("POST", "/admin/deprecate-by-type",
                                   body={"type": type, "dry_run": dry_run})


# ---------- Search (beyond recall) ----------

def _tool_search_tts(query: str, limit: int = 10) -> dict:
    """TTS-style search (text-to-semantic). Returns shorter recall pack."""
    return _CORTEX_CLIENT._request("GET", "/search/tts", params={"q": query, "limit": limit})


def _tool_search_hybrid(query: str, limit: int = 10) -> dict:
    """GET-style hybrid search. Same engine as POST /search but query-string driven."""
    return _CORTEX_CLIENT._request("GET", "/search/hybrid", params={"q": query, "limit": limit})


# ---------- Graph + relations ----------

def _tool_graph_khop(memory_id: str, k: int = 2, strength: float = 0.3, limit: int = 50) -> dict:
    """k-hop subgraph traversal from a seed chunk (GraphRAG)."""
    return _CORTEX_CLIENT._request("GET", "/graph/khop",
                                   params={"memory_id": memory_id, "k": k,
                                           "strength": strength, "limit": limit})


def _tool_graph_sample(limit: int = 50, type: str | None = None) -> dict:
    """Sample N chunks + their relations (for visualization)."""
    return _CORTEX_CLIENT._request("GET", "/graph/sample", params={"limit": limit, "type": type})


def _tool_graph_mine(similarity_threshold: float = 0.72, min_shared_tags: int = 2,
                     similar_batch: int = 200, per_anchor: int = 3) -> dict:
    """Batch-mine relations via similarity / shared-tag / same-source heuristics."""
    return _CORTEX_CLIENT._request("POST", "/graph/mine", body={
        "similarity_threshold": similarity_threshold,
        "min_shared_tags": min_shared_tags,
        "similar_batch": similar_batch,
        "per_anchor": per_anchor,
    })


def _tool_relations_delete_all(memory_id: str) -> dict:
    """Delete all relations involving a chunk."""
    return _CORTEX_CLIENT._request("DELETE", f"/relations/{memory_id}")


def _tool_relations_delete_one(memory_id: str, target_id: str) -> dict:
    """Delete one specific relation edge."""
    return _CORTEX_CLIENT.delete_relation(chunk_id=memory_id, target_id=target_id)


def _tool_relations_temporal_get(source_id: str) -> dict:
    """List temporal relations originating from a chunk."""
    return _CORTEX_CLIENT._request("GET", f"/relations/temporal/{source_id}")


# ---------- Links (typed) ----------

def _tool_links_get(source_id: str) -> dict:
    """Get outgoing links from a chunk."""
    return _CORTEX_CLIENT._request("GET", f"/links/{source_id}")


def _tool_links_access(source_id: str, target_id: str) -> dict:
    """Record an access event on a link (for usage stats)."""
    return _CORTEX_CLIENT._request("POST", "/links/access",
                                   body={"source_id": source_id, "target_id": target_id})


def _tool_links_path(source_id: str, target_id: str) -> dict:
    """Find shortest path between two chunks via links."""
    return _CORTEX_CLIENT._request("GET", f"/links/path/{source_id}/{target_id}")


def _tool_links_decay(factor: float = 0.95, dry_run: bool = False) -> dict:
    """Decay all link strengths by a factor (default 0.95).

    V6.6.0 fix (2026-10-31): this sent ``factor`` in a JSON body to an
    endpoint that read a query parameter named ``decay_rate``, so the
    requested rate was silently discarded and the decay always ran at
    the default. The endpoint now accepts ``factor`` directly, and both
    spellings mean the same thing (a multiplier to KEEP).
    """
    params = {"factor": factor}
    if dry_run:
        params["dry_run"] = "true"
    return _CORTEX_CLIENT._request("POST", "/links/decay", params=params)


# ---------- Working memory (per-session) ----------

def _tool_working_get(session_id: str) -> dict:
    """Get all KV pairs for a session."""
    return _CORTEX_CLIENT._request("GET", f"/working/{session_id}")


def _tool_working_get_key(session_id: str, key: str) -> dict:
    """Get one KV pair by session+key."""
    return _CORTEX_CLIENT._request("GET", f"/working/{session_id}/{key}")


def _tool_working_append(session_id: str, key: str | None = None, value: Any = None,
                         role: str | None = None, content: str | None = None) -> dict:
    """Append to working memory. Two shapes: (key,value) or (role,content)."""
    if key is not None and value is not None:
        body = {"key": key, "value": value}
    elif role is not None and content is not None:
        body = {"role": role, "content": content}
    else:
        raise ValueError("working_append requires either (key, value) or (role, content)")
    return _CORTEX_CLIENT._request("POST", f"/working/{session_id}/append", body=body)


# ---------- Forgetting / lifecycle (beyond forgetting_stats) ----------

def _tool_forgetting_expired(limit: int = 50) -> dict:
    """List chunks whose expires_at is in the past."""
    return _CORTEX_CLIENT._request("GET", "/forgetting/expired", params={"limit": limit})


def _tool_forgetting_expiring_soon(days: int = 7, limit: int = 50) -> dict:
    """List chunks expiring within N days (default 7)."""
    return _CORTEX_CLIENT._request("GET", "/forgetting/expiring-soon",
                                   params={"days": days, "limit": limit})


def _tool_forgetting_cleanup(force: bool = False) -> dict:
    """Run forgetting cleanup (15s timeout, archives expired chunks)."""
    return _CORTEX_CLIENT._request("POST", "/forgetting/cleanup", body={"force": force})


def _tool_forgetting_guided(force: bool = False) -> dict:
    """Access-driven tier upgrade: high-recall chunks move up a tier."""
    return _CORTEX_CLIENT._request("POST", "/forgetting/guided", body={"force": force})


# ---------- Audit / history / provenance / access ----------

def _tool_audit_query(memory_id: str | None = None, action: str | None = None,
                       resource_type: str | None = None, limit: int = 50) -> dict:
    """Query memory_access_audit log (read/write/update actions)."""
    return _CORTEX_CLIENT._request("GET", "/audit", params={
        "memory_id": memory_id, "action": action,
        "resource_type": resource_type, "limit": limit,
    })


def _tool_audit_log(action: str, memory_id: str | None = None,
                    resource_type: str | None = None, details: dict | None = None,
                    actor: str | None = None, session_id: str | None = None) -> dict:
    """Write a custom audit entry (action is required)."""
    body: dict = {"action": action}
    if memory_id: body["memory_id"] = memory_id
    if resource_type: body["resource_type"] = resource_type
    if details is not None: body["details"] = details
    if actor: body["actor"] = actor
    if session_id: body["session_id"] = session_id
    return _CORTEX_CLIENT._request("POST", "/audit/log", body=body)


def _tool_history_get(memory_id: str) -> list:
    """Get the version history of a chunk (creates/updates/merges). Uses SDK get_history."""
    return _CORTEX_CLIENT.get_history(memory_id=memory_id)


def _tool_provenance_get(memory_id: str) -> dict:
    """Get the provenance chain for a chunk (where it came from). Uses SDK get_provenance."""
    return _CORTEX_CLIENT.get_provenance(memory_id=memory_id)


def _tool_memories_refresh(memory_id: str) -> bool:
    """Record a refresh (search hit) on a chunk; recomputes relevance_score. Uses SDK refresh_memory."""
    return _CORTEX_CLIENT.refresh_memory(memory_id=memory_id)


def _tool_access_stats_for(memory_id: str) -> dict:
    """Get the latest access_stats row for one chunk."""
    return _CORTEX_CLIENT._request("GET", f"/access-stats/{memory_id}")


# ---------- Storage / cache / TTL ----------

def _tool_storage_evict(max_count: int | None = None, ratio: float = 0.05) -> dict:
    """Evict least-relevant chunks (importance<8 & tier!=long_term)."""
    body: dict = {"ratio": ratio}
    if max_count is not None: body["max_count"] = max_count
    return _CORTEX_CLIENT._request("POST", "/storage/evict", body=body)


def _tool_storage_enforce(force: bool = False) -> dict:
    """Enforce storage caps and invariants."""
    return _CORTEX_CLIENT._request("POST", "/storage/enforce", body={"force": force})


def _tool_cache_pr_clear() -> dict:
    """Clear the prompt cache."""
    return _CORTEX_CLIENT.clear_pr_cache()


def _tool_ttl_archive(days: int = 0) -> dict:
    """Archive chunks older than N days (0 = use default)."""
    return _CORTEX_CLIENT._request("POST", "/ttl/archive", body={"days": days})


def _tool_ttl_archived(limit: int = 50) -> dict:
    """List archived chunks."""
    return _CORTEX_CLIENT._request("GET", "/ttl/archived", params={"limit": limit})


# ---------- Taxonomy ----------

def _tool_taxonomy_delete(child_tag: str) -> dict:
    """Delete a taxonomy edge by child tag."""
    return _CORTEX_CLIENT._request("DELETE", f"/taxonomy/relations/{child_tag}")


# ---------- Core (user profile, beyond get) ----------

def _tool_core_list() -> dict:
    """List all user_ids that have a core profile."""
    return _CORTEX_CLIENT._request("GET", "/core")


def _tool_core_upsert(user_id: str, profile: dict, source: str = "agent") -> dict:
    """Create or replace a user core profile."""
    return _CORTEX_CLIENT._request("POST", f"/core/{user_id}",
                                   body={"profile": profile, "source": source})


# ---------- Episodes (beyond create) ----------

def _tool_episodes_get(memory_id: str) -> dict:
    """Fetch an episode by id (or by session_id as fallback)."""
    return _CORTEX_CLIENT.get_episodes_for_memory(memory_id=memory_id)


# ---------- Procedural (beyond learn/find — outcome recording) ----------

def _tool_procedural_outcome(proc_id: str, success: bool = True) -> dict:
    """Record success/failure for a procedure (increments counters)."""
    return _CORTEX_CLIENT._request("POST", f"/procedural/{proc_id}/outcome",
                                   body={"success": success})


# ---------- Hierarchy (beyond create/tree) ----------

def _tool_hierarchy_get(node_id: str) -> dict:
    """Get one hierarchy node by id."""
    return _CORTEX_CLIENT._request("GET", f"/hierarchy/{node_id}")


def _tool_hierarchy_enrich(chunk_id: str) -> dict:
    """Return hierarchy paths that contain the given chunk."""
    return _CORTEX_CLIENT._request("GET", f"/hierarchy/enrich/{chunk_id}")


# ---------- Media ----------

def _tool_media_create(media_type: str, content: str, mime_type: str = "text/plain",
                       metadata: dict | None = None) -> dict:
    """Create a media record (image/audio/etc metadata)."""
    return _CORTEX_CLIENT._request("POST", "/media", body={
        "media_type": media_type, "content": content, "mime_type": mime_type,
        "metadata": metadata or {},
    })


def _tool_media_search(query: str, limit: int = 5) -> dict:
    """Search media records by content ILIKE."""
    return _CORTEX_CLIENT._request("GET", "/media/search", params={"q": query, "limit": limit})


def _tool_media_by_type(media_type: str, limit: int = 50) -> dict:
    """List media records of a given type."""
    return _CORTEX_CLIENT._request("GET", f"/media/by-type/{media_type}", params={"limit": limit})


# ---------- Resources ----------

def _tool_resources_create(resource_type: str, title: str, content: str,
                           metadata: dict | None = None) -> dict:
    """Create a long-form resource record."""
    return _CORTEX_CLIENT._request("POST", "/resources", body={
        "resource_type": resource_type, "title": title, "content": content,
        "metadata": metadata or {},
    })


def _tool_resources_search(query: str, limit: int = 5,
                           resource_type: str | None = None) -> dict:
    """Search resources by title/content ILIKE."""
    return _CORTEX_CLIENT._request("GET", "/resources/search", params={
        "q": query, "limit": limit, "resource_type": resource_type,
    })


def _tool_resources_accessed(resource_id: str) -> dict:
    """Mark a resource as accessed (bumps access_count)."""
    return _CORTEX_CLIENT._request("POST", f"/resources/{resource_id}/accessed")


# ---------- Conflict (beyond upsert) ----------

def _tool_conflict_scan(lookback_days: int = 7, dry_run: bool = False,
                        max_buckets: int = 500) -> dict:
    """Periodic reconciliation: scan all source_key buckets."""
    return _CORTEX_CLIENT._request("POST", "/conflict/scan", body={
        "lookback_days": lookback_days, "dry_run": dry_run, "max_buckets": max_buckets,
    })


def _tool_conflict_log(user_id: str | None = None, source_key: str | None = None,
                       action: str | None = None, limit: int = 50) -> dict:
    """Query the supersede audit log."""
    return _CORTEX_CLIENT._request("GET", "/conflict/log", params={
        "user_id": user_id, "source_key": source_key, "action": action, "limit": limit,
    })


# ---------- Rumination (beyond run) ----------

def _tool_rumination_stats() -> dict:
    """Counts of patterns/heuristics produced by rumination."""
    return _CORTEX_CLIENT._request("GET", "/rumination/stats")


def _tool_rumination_abstract_heuristics(min_cluster_size: int = 5) -> dict:
    """Triggered abstract-heuristics (V6 ADR-067, requires LLM adapter)."""
    return _CORTEX_CLIENT._request("POST", "/rumination/abstract-heuristics",
                                   body={"min_cluster_size": min_cluster_size})


# ---------- Maintenance (beyond reconsolidate/merge_duplicates) ----------

def _tool_reindex() -> dict:
    """Reindex vector store (no-op for PG/HNSW)."""
    return _CORTEX_CLIENT._request("POST", "/reindex")


def _tool_chroma_cleanup() -> dict:
    """Chroma cleanup (PG backend: returns no-op)."""
    return _CORTEX_CLIENT._request("GET", "/chroma/cleanup")


# ---------- Compress ----------

def _tool_compress_run(max_count: int | None = None) -> dict:
    """Run compression pass on long chunks."""
    body: dict = {}
    if max_count is not None: body["max_count"] = max_count
    return _CORTEX_CLIENT._request("POST", "/compress/run", body=body)


def _tool_compress_get(source_chunk_id: str) -> dict:
    """Fetch compressed form of a source chunk."""
    return _CORTEX_CLIENT._request("GET", f"/compress/{source_chunk_id}")


# ---------- Consolidate (beyond run) ----------

def _tool_consolidate_extract(content: str, source_chunk_id: str | None = None,
                              user_id: str | None = None) -> dict:
    """Naive sentence-split fact extraction from a content blob."""
    body: dict = {"content": content}
    if source_chunk_id: body["source_chunk_id"] = source_chunk_id
    if user_id: body["user_id"] = user_id
    return _CORTEX_CLIENT._request("POST", "/consolidate/extract", body=body)


def _tool_consolidate_run(user_id: str | None = None, min_confidence: float = 0.5) -> dict:
    """Run consolidation (applies extracted facts to user core profile)."""
    body: dict = {"min_confidence": min_confidence}
    if user_id: body["user_id"] = user_id
    return _CORTEX_CLIENT._request("POST", "/consolidate/run", body=body)


def _tool_core_get(user_id: str) -> dict:
    """Get the user core profile by user_id."""
    return _CORTEX_CLIENT._request("GET", f"/core/{user_id}")


# ---------- Eval / bench ----------

def _tool_eval_feedback(feedback: list) -> dict:
    """Submit RLHF feedback for memories. Bumps relevance_score."""
    return _CORTEX_CLIENT._request("POST", "/eval/feedback", body={"feedback": feedback})


def _tool_bench_custom(questions: list) -> dict:
    """Submit a custom multi-hop benchmark."""
    return _CORTEX_CLIENT._request("POST", "/bench/multi-hop/custom",
                                   body={"questions": questions})


# ============================================================================
# Extended TOOLS list — 98 new entries, mirroring cortex's OpenAPI surface
# with the cortex_ prefix convention.
# ============================================================================

_EXTENDED_TOOLS: list[tuple[str, str, Callable[..., Any], dict]] = [
    # System / health / stats
    ("cortex_health", "Service health check (version, status, counts).",
     _tool_health, {}),
    ("cortex_stats", "Overall stats: total, tier breakdown, by-source.",
     _tool_stats, {}),
    ("cortex_stats_by_type", "Stats grouped by chunk type (knowledge/fact/preference/...).",
     _tool_stats_by_type, {}),
    ("cortex_stats_by_source", "Stats grouped by source label.",
     _tool_stats_by_source, {}),
    ("cortex_stats_by_status", "V6.2 explicit 3-bucket status breakdown.",
     _tool_stats_by_status, {}),
    ("cortex_stats_by_deprecated", "Stats by is_deprecated flag (governance view).",
     _tool_stats_by_deprecated, {}),
    ("cortex_stats_by_importance", "Stats grouped by importance bucket (0-2/3-5/6-8/9-10).",
     _tool_stats_by_importance, {}),
    ("cortex_stats_ops", "Operational counters (writes/reads/conflicts/etc).",
     _tool_stats_ops, {}),
    ("cortex_stats_embed_cache", "Embedding cache hit/miss stats (V6.2).",
     _tool_stats_embed_cache, {}),
    ("cortex_stats_health", "Detailed health: DB + vector index + cache + counters (V6.2).",
     _tool_stats_health, {}),
    ("cortex_stats_ops_reset", "Reset ops counters to zero.",
     _tool_stats_ops_reset, {}),
    ("cortex_mode", "Current cortex runtime mode (live/test).",
     _tool_mode, {}),
    ("cortex_endpoints_list", "List every registered endpoint with metadata.",
     _tool_endpoints_list, {}),
    ("cortex_endpoints_recommend", "Recommend endpoints for a scenario keyword.",
     _tool_endpoints_recommend, {"scenario": str}),
    ("cortex_dashboard", "Dashboard JSON: stats + 10 most recent chunks.",
     _tool_dashboard, {}),
    ("cortex_dashboard_html", "Dashboard HTML page (4 tabs).",
     _tool_dashboard_html, {}),

    # Memory chunks
    ("cortex_chunks_create_batch", "Bulk create up to 500 chunks.",
     _tool_chunks_create_batch, {"chunks": list}),
    ("cortex_chunks_update", "Full-replace a chunk (PUT-style).",
     _tool_chunks_update, {"chunk_id": str, "content": str, "type": str,
                           "tags": list, "source": str, "importance": int}),
    ("cortex_chunks_patch", "Partial-update a chunk (PATCH-style).",
     _tool_chunks_patch, {"chunk_id": str, "content": str, "type": str,
                          "tags": list, "source": str, "importance": int}),
    ("cortex_chunks_list", "List chunks with filters (type, tag, limit, offset).",
     _tool_chunks_list, {"type": str, "tag": str, "limit": int, "offset": int}),
    ("cortex_admin_deprecate_by_type", "Admin: bulk deprecate all chunks of a type.",
     _tool_admin_deprecate_by_type, {"type": str, "dry_run": bool}),

    # Search
    ("cortex_search_tts", "TTS-style search (text-to-semantic).",
     _tool_search_tts, {"query": str, "limit": int}),
    ("cortex_search_hybrid", "GET-style hybrid search.",
     _tool_search_hybrid, {"query": str, "limit": int}),

    # Graph + relations
    ("cortex_graph_khop", "k-hop subgraph traversal from a seed chunk (GraphRAG).",
     _tool_graph_khop, {"memory_id": str, "k": int, "strength": float, "limit": int}),
    ("cortex_graph_sample", "Sample N chunks + their relations (for visualization).",
     _tool_graph_sample, {"limit": int, "type": str}),
    ("cortex_graph_mine", "Batch-mine relations via heuristics.",
     _tool_graph_mine, {"similarity_threshold": float, "min_shared_tags": int,
                        "similar_batch": int, "per_anchor": int}),
    ("cortex_relations_get", "List relations for a chunk (uses SDK get_relations).",
     _CORTEX_CLIENT.get_relations, {"chunk_id": str}),
    ("cortex_relations_delete_all", "Delete all relations involving a chunk.",
     _tool_relations_delete_all, {"memory_id": str}),
    ("cortex_relations_delete_one", "Delete one specific relation edge.",
     _tool_relations_delete_one, {"memory_id": str, "target_id": str}),
    ("cortex_relations_temporal_create", "Create a temporal (time-ordered) relation.",
     _CORTEX_CLIENT.create_temporal_relation,
     {"source_id": str, "target_id": str, "relation_type": str, "strength": float}),
    ("cortex_relations_temporal_get", "List temporal relations originating from a chunk.",
     _tool_relations_temporal_get, {"source_id": str}),

    # Links
    ("cortex_links_create", "Create a typed link between two chunks (uses SDK create_link).",
     _CORTEX_CLIENT.create_link,
     {"source_id": str, "target_id": str, "relation": str, "strength": float}),
    ("cortex_links_get", "Get outgoing links from a chunk.",
     _tool_links_get, {"source_id": str}),
    ("cortex_links_access", "Record an access event on a link (for usage stats).",
     _tool_links_access, {"source_id": str, "target_id": str}),
    ("cortex_links_path", "Find shortest path between two chunks via links.",
     _tool_links_path, {"source_id": str, "target_id": str}),
    ("cortex_links_decay", "Decay all link strengths by a factor (default 0.95).",
     _tool_links_decay, {"factor": float}),

    # Working memory
    ("cortex_working_set", "Set a KV pair in working memory for a session (uses SDK set_working_memory).",
     _CORTEX_CLIENT.set_working_memory,
     {"session_id": str, "key": str, "value": str}),
    ("cortex_working_get", "Get all KV pairs for a session.",
     _tool_working_get, {"session_id": str}),
    ("cortex_working_get_key", "Get one KV pair by session+key.",
     _tool_working_get_key, {"session_id": str, "key": str}),
    ("cortex_working_append", "Append to working memory (key/value OR role/content).",
     _tool_working_append, {"session_id": str, "key": str, "value": str,
                            "role": str, "content": str}),
    ("cortex_working_context", "Build full LLM context for a session (uses SDK build_context).",
     _CORTEX_CLIENT.build_context, {"session_id": str}),

    # Forgetting
    ("cortex_forgetting_stats", "Counts: total/expired + tier breakdown (uses SDK forgetting_stats).",
     _CORTEX_CLIENT.forgetting_stats, {}),
    ("cortex_forgetting_expired", "List chunks whose expires_at is in the past.",
     _tool_forgetting_expired, {"limit": int}),
    ("cortex_forgetting_expiring_soon", "List chunks expiring within N days (default 7).",
     _tool_forgetting_expiring_soon, {"days": int, "limit": int}),
    ("cortex_forgetting_cleanup", "Run forgetting cleanup (15s timeout).",
     _tool_forgetting_cleanup, {"force": bool}),
    ("cortex_forgetting_guided", "Access-driven tier upgrade.",
     _tool_forgetting_guided, {"force": bool}),

    # Audit / access
    ("cortex_audit_query", "Query memory_access_audit log.",
     _tool_audit_query, {"memory_id": str, "action": str,
                         "resource_type": str, "limit": int}),
    ("cortex_audit_log", "Write a custom audit entry (action is required).",
     _tool_audit_log, {"action": str, "memory_id": str, "resource_type": str,
                       "details": dict, "actor": str, "session_id": str}),
    ("cortex_history_get", "Get the version history of a chunk (uses SDK get_history).",
     _tool_history_get, {"memory_id": str}),
    ("cortex_provenance_get", "Get the provenance chain for a chunk (uses SDK get_provenance).",
     _tool_provenance_get, {"memory_id": str}),
    ("cortex_access_stats", "List access_stats rows (uses SDK access_stats).",
     _CORTEX_CLIENT.access_stats, {"memory_id": str}),
    ("cortex_access_stats_for", "Get the latest access_stats row for one chunk.",
     _tool_access_stats_for, {"memory_id": str}),
    ("cortex_memories_refresh", "Record a refresh (search hit) on a chunk (uses SDK refresh_memory).",
     _tool_memories_refresh, {"memory_id": str}),

    # Storage / cache / TTL
    ("cortex_storage_health", "Storage-layer health (uses SDK storage_health).",
     _CORTEX_CLIENT.storage_health, {}),
    ("cortex_storage_evict", "Evict least-relevant chunks.",
     _tool_storage_evict, {"max_count": int, "ratio": float}),
    ("cortex_storage_enforce", "Enforce storage caps and invariants.",
     _tool_storage_enforce, {"force": bool}),
    ("cortex_cache_pr_stats", "Prompt-cache stats (uses SDK pr_cache_stats).",
     _CORTEX_CLIENT.pr_cache_stats, {}),
    ("cortex_cache_pr_clear", "Clear the prompt cache.",
     _tool_cache_pr_clear, {}),
    ("cortex_ttl_archive", "Archive chunks older than N days.",
     _tool_ttl_archive, {"days": int}),
    ("cortex_ttl_archived", "List archived chunks.",
     _tool_ttl_archived, {"limit": int}),

    # Taxonomy
    ("cortex_taxonomy_relations", "List the tag taxonomy (uses SDK taxonomy_relations).",
     _CORTEX_CLIENT.taxonomy_relations, {}),
    ("cortex_taxonomy_ancestors", "List ancestor tags for a child tag (uses SDK).",
     _CORTEX_CLIENT.taxonomy_ancestors, {"child_tag": str}),
    ("cortex_taxonomy_delete", "Delete a taxonomy edge by child tag.",
     _tool_taxonomy_delete, {"child_tag": str}),

    # Core
    ("cortex_core_get", "Get the user core profile by user_id.",
     _tool_core_get, {"user_id": str}),
    ("cortex_core_list", "List all user_ids that have a core profile.",
     _tool_core_list, {}),
    ("cortex_core_upsert", "Create or replace a user core profile.",
     _tool_core_upsert, {"user_id": str, "profile": dict, "source": str}),

    # Episodes
    ("cortex_episodes_create", "Create an episode (uses SDK remember_episode).",
     _CORTEX_CLIENT.remember_episode,
     {"session_id": str, "title": str, "summary": str, "parent_chunk_id": str}),
    ("cortex_episodes_get", "Fetch an episode by id (or by session_id as fallback).",
     _tool_episodes_get, {"memory_id": str}),

    # Procedural
    ("cortex_procedural_create", "Learn a trigger->action procedure (uses SDK learn_procedure).",
     _CORTEX_CLIENT.learn_procedure,
     {"name": str, "trigger": str, "action": str, "description": str}),
    ("cortex_procedural_search", "Find procedures matching a query (uses SDK find_procedure).",
     _CORTEX_CLIENT.find_procedure,
     {"trigger_query": str, "limit": int}),
    ("cortex_procedural_outcome", "Record success/failure for a procedure.",
     _tool_procedural_outcome, {"proc_id": str, "success": bool}),

    # Hierarchy
    ("cortex_hierarchy_create", "Create a hierarchy node (uses SDK create_hierarchy_node).",
     _CORTEX_CLIENT.create_hierarchy_node,
     {"level": str, "title": str, "parent_id": str, "metadata": dict}),
    ("cortex_hierarchy_get", "Get one hierarchy node by id.",
     _tool_hierarchy_get, {"node_id": str}),
    ("cortex_hierarchy_enrich", "Return hierarchy paths that contain the given chunk.",
     _tool_hierarchy_enrich, {"chunk_id": str}),
    ("cortex_hierarchy_tree", "Get the full subtree rooted at a node (uses SDK get_hierarchy_tree).",
     _CORTEX_CLIENT.get_hierarchy_tree,
     {"root_id": str, "max_depth": int}),

    # Media
    ("cortex_media_create", "Create a media record (image/audio/etc metadata).",
     _tool_media_create, {"media_type": str, "content": str,
                          "mime_type": str, "metadata": dict}),
    ("cortex_media_search", "Search media records by content ILIKE.",
     _tool_media_search, {"query": str, "limit": int}),
    ("cortex_media_by_type", "List media records of a given type.",
     _tool_media_by_type, {"media_type": str, "limit": int}),

    # Resources
    ("cortex_resources_create", "Create a long-form resource record.",
     _tool_resources_create, {"resource_type": str, "title": str,
                              "content": str, "metadata": dict}),
    ("cortex_resources_search", "Search resources by title/content ILIKE.",
     _tool_resources_search, {"query": str, "limit": int, "resource_type": str}),
    ("cortex_resources_accessed", "Mark a resource as accessed (bumps access_count).",
     _tool_resources_accessed, {"resource_id": str}),

    # Conflict
    ("cortex_conflict_scan", "Periodic reconciliation: scan all source_key buckets.",
     _tool_conflict_scan, {"lookback_days": int, "dry_run": bool, "max_buckets": int}),
    ("cortex_conflict_log", "Query the supersede audit log.",
     _tool_conflict_log, {"user_id": str, "source_key": str, "action": str, "limit": int}),

    # Rumination
    ("cortex_rumination_stats", "Counts of patterns/heuristics produced by rumination.",
     _tool_rumination_stats, {}),
    ("cortex_rumination_run", "Cluster similar memories, detect contradictions, merge fragments (uses SDK ruminate).",
     _CORTEX_CLIENT.ruminate, {"min_cluster_size": int}),
    ("cortex_rumination_abstract_heuristics", "Triggered abstract-heuristics (requires LLM).",
     _tool_rumination_abstract_heuristics, {"min_cluster_size": int}),

    # Maintenance
    ("cortex_reconsolidate", "Strengthen/weaken memories based on access + age (uses SDK).",
     _CORTEX_CLIENT.reconsolidate, {"memory_id": str}),
    ("cortex_merge_duplicates", "Merge near-duplicate chunks (uses SDK merge_duplicates).",
     _CORTEX_CLIENT.merge_duplicates, {"threshold": float}),
    ("cortex_reindex", "Reindex vector store (no-op for PG/HNSW).",
     _tool_reindex, {}),
    ("cortex_chroma_cleanup", "Chroma cleanup (PG backend: no-op).",
     _tool_chroma_cleanup, {}),

    # Compress
    ("cortex_compress_run", "Run compression pass on long chunks.",
     _tool_compress_run, {"max_count": int}),
    ("cortex_compress_get", "Fetch compressed form of a source chunk.",
     _tool_compress_get, {"source_chunk_id": str}),

    # Consolidate
    ("cortex_consolidate_extract", "Naive sentence-split fact extraction.",
     _tool_consolidate_extract, {"content": str, "source_chunk_id": str, "user_id": str}),
    ("cortex_consolidate_run", "Run consolidation (applies extracted facts to user core).",
     _tool_consolidate_run, {"user_id": str, "min_confidence": float}),
    ("cortex_consolidate_facts", "List extracted facts (uses SDK consolidate_facts).",
     _CORTEX_CLIENT.consolidate_facts, {"user_id": str, "min_confidence": float}),

    # Eval / bench
    ("cortex_eval_feedback", "Submit RLHF feedback for memories.",
     _tool_eval_feedback, {"feedback": list}),
    ("cortex_bench_multi_hop", "Run multi-hop reasoning benchmark (uses SDK bench_multi_hop).",
     _CORTEX_CLIENT.bench_multi_hop, {"queries": list}),
    ("cortex_bench_custom", "Submit a custom multi-hop benchmark.",
     _tool_bench_custom, {"questions": list}),
]

# Sanity: 98 new tools, on top of the 6 existing friendly ones = 104 total.
assert len(_EXTENDED_TOOLS) == 98, f"expected 98 new tools, got {len(_EXTENDED_TOOLS)}"
TOOLS.extend(_EXTENDED_TOOLS)


# === FastMCP instance ===

def _offload_sync_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a *sync* tool handler so it runs in a worker thread.

    Why this exists (V6.6.0 fix, 2026-10-30)
    ------------------------------------------
    Every tool handler in this module talks to Cortex over HTTP through
    ``_CORTEX_CLIENT`` (``http://127.0.0.1:1880``) — i.e. the handler makes
    a **loopback request to the very server that is executing the MCP call**.

    FastMCP invokes sync handlers inline on the event loop. A blocking
    ``urllib.request.urlopen`` to ourselves therefore deadlocks: the loop is
    busy waiting for a response that only the blocked loop could serve.
    The SDK's 30s timeout then fires and the tool returns
    ``{"error": "timed out"}`` with ``isError=False``, so the failure looks
    like a *successful* call with an error payload.

    Observed impact on 127.0.0.1: every MCP tool call took exactly 30.0s
    and returned ``{"error": "timed out"}``. That silently broke the whole
    scheduled-maintenance path (``cortex_maint_daily.py`` /
    ``cortex_maint_weekly.py`` call these tools over MCP) — every daily run
    logged ``OK <tool> -> {"error": "timed out"}`` and ``errors=0``, so the
    cron job looked healthy while doing nothing.

    Running the sync handler in a worker thread keeps the event loop free to
    serve the loopback request, so the call completes normally.

    Async handlers are returned untouched (they already yield the loop).

    ``functools.wraps`` preserves ``__name__``/``__doc__``/``__wrapped__``,
    so FastMCP still derives the same input schema — verified: the tool
    catalog is byte-identical before and after this wrapping.
    """
    import functools
    import inspect

    if inspect.iscoroutinefunction(handler):
        return handler

    try:
        import anyio
    except ImportError:  # pragma: no cover - anyio ships with starlette
        # Without anyio we cannot offload; register the handler as-is.
        return handler

    @functools.wraps(handler)
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(
            functools.partial(handler, *args, **kwargs)
        )

    return _wrapper


def _build_mcp() -> FastMCP:
    """Build the FastMCP server with the six core Cortex tools."""
    mcp = FastMCP(
        name="memory-cortex",
        instructions=(
            "Memory Cortex MCP surface, mounted at /mcp on the Cortex "
            "FastAPI app. 104 tools total: 6 friendly aliases (cortex_remember, "
            "cortex_recall, cortex_forget, cortex_get, cortex_introspect, "
            "cortex_conflict_upsert) for the most common operations, plus 98 "
            "technical tools (cortex_<group>_<action>) covering the full "
            "OpenAPI surface: stats, search, graph, relations, links, working, "
            "forgetting, audit, hierarchy, procedural, episodes, media, "
            "resources, conflict, rumination, maintenance, compress, consolidate, "
            "eval/bench, taxonomy, storage, etc. Tags + importance (1..10) shape "
            "the tier/TTL policy server-side."
        ),
    )
    # fastmcp 3.x dropped the (handler, name=, description=) form of
    # add_tool. Decorate each handler with @mcp.tool(name=, description=)
    # before binding so the MCP tool catalog exposes our 6 names.
    for tool_name, description, handler, _params in TOOLS:
        decorated = mcp.tool(name=tool_name, description=description)(
            _offload_sync_handler(handler)
        )
        # Decorator returns the (now-bound) FunctionTool; nothing else to do.

    @mcp.resource("cortex://breaker")
    def breaker_state() -> str:
        """Current circuit-breaker state of the local Cortex client."""
        return json.dumps(cortex_client.breaker_state(), indent=2)

    return mcp


_MCP_INSTANCE = _build_mcp()

# Module-level cached sub-app + lifespan. fastmcp 3.x's ``http_app()`` returns
# a fresh ``StarletteWithLifespan`` instance each call, but its lifespan sets
# up the singleton ``FastMCPStreamableHTTPSessionManager`` on
# ``streamable_http_app`` (see fastmcp.server.http.create_streamable_http_app).
# Building it once at import time means both ``get_mcp_app()`` and
# ``get_mcp_lifespan()`` share the same sub-app instance — no duplicate
# session managers, no surprise when /mcp is mounted.
_MCP_HTTP_APP = _MCP_INSTANCE.http_app(
    path="/",
    transport="streamable-http",
    json_response=False,
    stateless_http=False,
)
_MCP_HTTP_LIFESPAN = _MCP_HTTP_APP.router.lifespan_context


# === ASGI app ===
#
# fastmcp 3.x exposes the whole transport via ``mcp.http_app(path='/mcp',
# transport='streamable-http')`` which returns a Starlette sub-app *with a
# lifespan* attached (so FastMCP's task group is initialized when Starlette
# starts the sub-app). We mount that ASGI app at ``/mcp`` on the parent
# FastAPI app via ``app.mount('/mcp', mcp_app)`` and let Starlette handle
# the trailing-slash / root_path quirks natively.
#
# The earlier (fastmcp 2.x) implementation hand-rolled a ``_MCPASGIBridge``
# + ``StreamableHTTPSessionManager`` to work around
#   1) Starlette's ``Mount`` setting ``root_path`` to ``/mcp`` while
#      ``get_route_path`` strips it back out;
#   2) FastMCP's lazy session-manager init not being started by the
#      parent FastAPI lifespan.
# fastmcp 3.x's ``http_app`` does both correctly, so the bridge is gone.

PREFIX = "/mcp"


def get_mcp_app() -> Any:
    """Return the FastMCP streamable-http ASGI sub-app (cached singleton)."""
    return _MCP_HTTP_APP


def add_mcp_route(app: Any) -> None:
    """Mount the FastMCP ASGI sub-app at ``/mcp`` on the parent FastAPI app."""
    app.mount(PREFIX, get_mcp_app())


def get_mcp_lifespan() -> Callable[..., AsyncIterator[None]]:
    """Return the FastMCP sub-app's lifespan (cached singleton).

    fastmcp 3.x's ``http_app`` returns a ``StarletteWithLifespan`` whose
    ``router.lifespan_context`` is an ``@asynccontextmanager``-decorated
    function. When invoked, it:

    1. Builds a ``FastMCPStreamableHTTPSessionManager`` and assigns it to
       ``streamable_http_app.session_manager``;
    2. Enters an ``async with server._lifespan_manager(),
       streamable_http_app.session_manager.run():`` block — starting
       FastMCP's anyio task group;
    3. On exit, terminates every active streamable-HTTP transport before
       the task group is cancelled (avoids the
       "ASGI callable returned without completing response" warning when
       SSE streams are mid-flight — see PrefectHQ/fastmcp#3025).

    ``api_pg.py`` wraps this with its ``_combined_lifespan`` async
    context manager so the FastAPI app's router.lifespan_context calls
    ``get_mcp_lifespan()(app_)`` once at startup.
    """
    return _MCP_HTTP_LIFESPAN


def get_server_info() -> dict[str, Any]:
    """Metadata for the Cortex /stats endpoint to surface MCP presence."""
    tool_names = [t[0] for t in TOOLS]
    return {
        "mcp_enabled": True,
        "mcp_path": "/mcp",
        "transport": "streamable-http",
        "tool_count": len(TOOLS),
        "tools": sorted(tool_names),
    }
