"""
cortex HTTP API client for Claude Code.

Covers all 89 endpoints exposed by api_pg.py v6.5.1 (validated 2026-09-08).
Live server: 127.0.0.1:1880 (2653 chunks, 15207 relations).

Usage:
    from claude_cortex_client import Cortex
    cx = Cortex()
    cx.health()
    cx.remember("...")
    cx.recall("...")
    cx.dashboard()
    cx.run_claude_smoke()        # full self-test of every endpoint group

Destructive methods (mutate DB / trigger long ops) are suffixed `_safe` or
explicitly named; they all run behind a guard so a stray call from a script
won't accidentally rewrite history. Pass `confirm=True` to opt in.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import requests

DEFAULT_BASE = os.getenv("CORTEX_URL", "http://127.0.0.1:1880")
TIMEOUT = float(os.getenv("CORTEX_TIMEOUT", "15"))
LONG_TIMEOUT = float(os.getenv("CORTEX_LONG_TIMEOUT", "60"))


class CortexError(RuntimeError):
    """Non-2xx response from cortex."""


class Cortex:
    # ---------- ctor ----------
    def __init__(self, base_url: str = DEFAULT_BASE, timeout: float = TIMEOUT):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()

    # ---------- low-level ----------
    def _req(self, method: str, path: str, *, timeout: float | None = None, **kw) -> Any:
        url = f"{self.base}{path}"
        kw.setdefault("timeout", timeout or self.timeout)
        r = self.s.request(method, url, **kw)
        if not r.ok:
            raise CortexError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        if not r.content:
            return {}
        # /metrics returns Prometheus text
        if path.endswith("/metrics") or "text/plain" in r.headers.get("Content-Type", ""):
            return r.text
        return r.json()

    def _get(self, path: str, **kw): return self._req("GET", path, **kw)
    def _post(self, path: str, **kw): return self._req("POST", path, **kw)
    def _patch(self, path: str, **kw): return self._req("PATCH", path, **kw)
    def _delete(self, path: str, **kw): return self._req("DELETE", path, **kw)

    # ════════════════════════════════════════════════════════════════════════
    # Group 1: Core chunk CRUD
    # ════════════════════════════════════════════════════════════════════════
    def remember(self, content: str, *, type: str = "general",
                 tags: Iterable[str] = (), source: str = "claude-code",
                 importance: int = 5, metadata: dict | None = None,
                 embedding_model: str | None = None) -> dict:
        body: dict[str, Any] = {"content": content, "type": type,
                                "tags": list(tags), "source": source,
                                "importance": importance, "metadata": metadata or {}}
        if embedding_model:
            body["embedding_model"] = embedding_model
        return self._post("/chunks", json=body)

    def remember_many(self, items: list[dict], embedding_model: str | None = None) -> dict:
        """POST /chunks/batch — body {chunks:[...], embedding_model?}; max 500."""
        body: dict[str, Any] = {"chunks": items}
        if embedding_model:
            body["embedding_model"] = embedding_model
        return self._post("/chunks/batch", json=body)

    def get_chunk(self, chunk_id: str, *, include_embedding: bool = True) -> dict:
        return self._get(f"/chunks/{chunk_id}", params={"include_embedding": include_embedding})

    def update_chunk(self, chunk_id: str, content: str, *, type: str = "general",
                     tags: Iterable[str] = (), source: str = "claude-code",
                     importance: int = 5, metadata: dict | None = None,
                     embedding_model: str | None = None) -> dict:
        """POST /chunks/{id} — full replace; re-derives tier+TTL."""
        body: dict[str, Any] = {"content": content, "type": type,
                                "tags": list(tags), "source": source,
                                "importance": importance, "metadata": metadata or {}}
        if embedding_model:
            body["embedding_model"] = embedding_model
        return self._post(f"/chunks/{chunk_id}", json=body)

    def patch_chunk(self, chunk_id: str, **fields) -> dict:
        """PATCH /chunks/{id} — partial update. Any of content/type/tags/source/importance/metadata."""
        return self._patch(f"/chunks/{chunk_id}", json=fields)

    def forget(self, chunk_id: str) -> dict:
        """DELETE /chunks/{id} — HARD delete + relations cleanup."""
        return self._delete(f"/chunks/{chunk_id}")

    def list(self, *, type: str | None = None, tags: str | None = None,
             match_all: bool = True, min_importance: int | None = None,
             limit: int = 50, offset: int = 0, source: str | None = None,
             include_embedding: bool = False) -> dict:
        """GET /list — tags is CSV string."""
        kw = {"limit": limit, "offset": offset, "match_all": match_all}
        if type: kw["type"] = type
        if tags: kw["tags"] = tags
        if min_importance is not None: kw["min_importance"] = min_importance
        if source: kw["source"] = source
        if include_embedding: kw["include_embedding"] = True
        return self._get("/list", params=kw)

    # ════════════════════════════════════════════════════════════════════════
    # Group 2: Search variants
    # ════════════════════════════════════════════════════════════════════════
    def recall(self, query: str, *, limit: int = 5,
               type: str | None = None, tags: Iterable[str] | None = None,
               min_importance: int = 0, include_related: bool = False,
               hybrid: bool = True, boost_relevance: bool = True,
               top_lexical: int = 24, top_vector: int = 24, rrf_k: int = 60,
               embedding_model: str | None = None) -> list[dict]:
        """POST /search — returns the results[] array."""
        body: dict[str, Any] = {"query": query, "limit": limit, "hybrid": hybrid,
                                "min_importance": min_importance,
                                "include_related": include_related,
                                "boost_relevance": boost_relevance,
                                "top_lexical": top_lexical, "top_vector": top_vector,
                                "rrf_k": rrf_k}
        if type: body["type"] = type
        if tags: body["tags"] = list(tags)
        if embedding_model: body["embedding_model"] = embedding_model
        out = self._post("/search", json=body)
        return out.get("results", [])

    def recall_tts(self, q: str, limit: int = 10) -> dict:
        """GET /search/tts — returns full envelope."""
        return self._get("/search/tts", params={"q": q, "limit": limit})

    def recall_hybrid(self, q: str, limit: int = 10) -> dict:
        """GET /search/hybrid — alias wrapper, returns full envelope."""
        return self._get("/search/hybrid", params={"q": q, "limit": limit})

    # ════════════════════════════════════════════════════════════════════════
    # Group 3: Stats & introspection
    # ════════════════════════════════════════════════════════════════════════
    def health(self) -> dict: return self._get("/health")
    def stats(self) -> dict: return self._get("/stats")
    def stats_by_type(self) -> dict: return self._get("/stats/by-type")
    def stats_by_source(self) -> dict: return self._get("/stats/by-source")
    def stats_by_status(self) -> dict: return self._get("/stats/by-status")
    def stats_by_deprecated(self) -> dict: return self._get("/stats/by-deprecated")
    def stats_by_importance(self) -> dict: return self._get("/stats/by-importance")
    def stats_ops(self) -> dict: return self._get("/stats/ops")
    def stats_embed_cache(self) -> dict: return self._get("/stats/embed-cache")
    def stats_health(self) -> dict: return self._get("/stats/health")
    def reset_ops(self) -> dict: return self._post("/stats/ops/reset")
    def list_endpoints(self) -> dict: return self._get("/endpoints")
    def recommend(self, scenario: str) -> dict: return self._get(f"/endpoints/recommend/{scenario}")
    def mode(self) -> dict: return self._get("/mode")
    def metrics(self) -> str: return self._get("/metrics")  # prometheus text

    # ════════════════════════════════════════════════════════════════════════
    # Group 4: Graph
    # ════════════════════════════════════════════════════════════════════════
    def graph_khop(self, memory_id: str, *, k: int = 2,
                   min_strength: float = 0.5, limit: int = 12) -> dict:
        return self._get("/graph/khop", params={"memory_id": memory_id, "k": k,
                                                "min_strength": min_strength, "limit": limit})

    def graph_sample(self, *, limit: int = 50, type: str | None = None,
                     category: str | None = None) -> dict:
        kw = {"limit": limit}
        if type: kw["type"] = type
        if category: kw["category"] = category
        return self._get("/graph/sample", params=kw)

    def graph_mine(self, *, similarity_threshold: float = 0.72,
                   min_shared_tags: int = 2, similar_batch: int = 200,
                   per_anchor: int = 3, confirm: bool = False) -> dict:
        """POST /graph/mine — bulk 3-pass relation discovery. SLOW + mutates."""
        if not confirm:
            raise PermissionError("graph_mine mutates memory_relations; pass confirm=True")
        return self._post("/graph/mine", json={"similarity_threshold": similarity_threshold,
                                               "min_shared_tags": min_shared_tags,
                                               "similar_batch": similar_batch,
                                               "per_anchor": per_anchor},
                           timeout=LONG_TIMEOUT)

    # ════════════════════════════════════════════════════════════════════════
    # Group 5: Relations & links
    # ════════════════════════════════════════════════════════════════════════
    def relations_batch(self, ids: list[str], *, depth: int = 1,
                        max_per_node: int = 10) -> dict:
        return self._get("/relations/batch", params={"ids": ",".join(ids),
                                                     "depth": depth,
                                                     "max_per_node": max_per_node})

    def relations_for(self, memory_id: str, *, min_strength: float = 0.0) -> dict:
        return self._get(f"/relations/{memory_id}", params={"min_strength": min_strength})

    def delete_relations(self, memory_id: str) -> dict:
        return self._delete(f"/relations/{memory_id}")

    def delete_relation(self, memory_id: str, target_id: str) -> dict:
        return self._delete(f"/relations/{memory_id}/{target_id}")

    def create_temporal_relation(self, source_id: str, target_id: str,
                                 *, relation_type: str = "related",
                                 strength: float = 0.5) -> dict:
        return self._post("/relations/temporal", json={"source_id": source_id,
                                                       "target_id": target_id,
                                                       "relation_type": relation_type,
                                                       "strength": strength})

    def temporal_relations(self, source_id: str, *,
                           at_time: str | None = None,
                           relation_type: str | None = None) -> dict:
        kw: dict[str, Any] = {}
        if at_time: kw["at_time"] = at_time
        if relation_type: kw["relation_type"] = relation_type
        return self._get(f"/relations/temporal/{source_id}", params=kw)

    def link_create(self, source_id: str, target_id: str, *,
                    link_type: str = "related", strength: float = 0.5,
                    source: str = "manual") -> dict:
        return self._post("/links/create", json={"source_id": source_id,
                                                 "target_id": target_id,
                                                 "link_type": link_type,
                                                 "strength": strength,
                                                 "source": source})

    def links_for(self, source_id: str) -> dict:
        return self._get(f"/links/{source_id}")

    def link_access(self, source_id: str, target_id: str) -> dict:
        return self._post("/links/access", json={"source_id": source_id, "target_id": target_id})

    def link_path(self, source_id: str, target_id: str) -> dict:
        return self._get(f"/links/path/{source_id}/{target_id}")

    def link_decay(self, decay_rate: float = 0.01) -> dict:
        return self._post("/links/decay", params={"decay_rate": decay_rate})

    # ════════════════════════════════════════════════════════════════════════
    # Group 6: Working memory
    # ════════════════════════════════════════════════════════════════════════
    def working_set(self, session_id: str, key: str, value: Any) -> dict:
        return self._post("/working/set", json={"session_id": session_id, "key": key, "value": value})

    def working_context(self, session_id: str) -> dict:
        """GET /working/{sid}/context — returns {working, conversation}."""
        return self._get(f"/working/{session_id}/context")

    def working_append(self, session_id: str, *, key: str | None = None, value: Any = None,
                       role: str | None = None, content: str | None = None) -> dict:
        """POST /working/{sid}/append — two payload shapes."""
        if key is not None and value is not None:
            return self._post(f"/working/{session_id}/append", json={"key": key, "value": value})
        if role is not None and content is not None:
            return self._post(f"/working/{session_id}/append", json={"role": role, "content": content})
        raise ValueError("pass either (key,value) or (role,content)")

    def working_get(self, session_id: str) -> dict:
        return self._get(f"/working/{session_id}")

    def working_get_key(self, session_id: str, key: str) -> dict:
        return self._get(f"/working/{session_id}/{key}")

    # ════════════════════════════════════════════════════════════════════════
    # Group 7: Forgetting lifecycle
    # ════════════════════════════════════════════════════════════════════════
    def forgetting_stats(self) -> dict: return self._get("/forgetting/stats")
    def forgetting_expired(self, limit: int = 50) -> dict:
        return self._get("/forgetting/expired", params={"limit": limit})
    def forgetting_expiring_soon(self, days: int = 7, limit: int = 50) -> dict:
        return self._get("/forgetting/expiring-soon", params={"days": days, "limit": limit})

    def forgetting_cleanup(self, force: bool = False, confirm: bool = False) -> dict:
        """POST /forgetting/cleanup — DELETES expired chunks (no undo)."""
        if not confirm:
            raise PermissionError("destructive: deletes expired chunks; pass confirm=True")
        return self._post("/forgetting/cleanup", params={"force": force}, timeout=LONG_TIMEOUT)

    def forgetting_guided(self, force: bool = False, confirm: bool = False) -> dict:
        """POST /forgetting/guided — adjusts tier/TTL based on access. Mutates."""
        if not confirm:
            raise PermissionError("mutates tier+TTL; pass confirm=True")
        return self._post("/forgetting/guided", params={"force": force}, timeout=LONG_TIMEOUT)

    # ════════════════════════════════════════════════════════════════════════
    # Group 8: Audit / history / provenance
    # ════════════════════════════════════════════════════════════════════════
    def audit(self, *, limit: int = 50, memory_id: str | None = None,
              action: str | None = None, actor: str | None = None,
              since: str | None = None, until: str | None = None,
              resource_type: str | None = None) -> dict:
        kw: dict[str, Any] = {"limit": limit}
        if memory_id: kw["memory_id"] = memory_id
        if action: kw["action"] = action
        if actor: kw["actor"] = actor
        if since: kw["since"] = since
        if until: kw["until"] = until
        if resource_type: kw["resource_type"] = resource_type
        return self._get("/audit", params=kw)

    def audit_log(self, *, action: str, memory_id: str | None = None,
                  resource_type: str = "chunk", details: dict | None = None,
                  actor: str | None = None, session_id: str | None = None) -> dict:
        kw = {"action": action, "resource_type": resource_type}
        body: dict[str, Any] = {}
        if memory_id: body["memory_id"] = memory_id
        if details: body["details"] = details
        if actor: body["actor"] = actor
        if session_id: body["session_id"] = session_id
        return self._post("/audit/log", params=kw, json=body)

    def history(self, memory_id: str) -> dict:
        return self._get(f"/history/{memory_id}")

    def provenance(self, memory_id: str) -> dict:
        return self._get(f"/provenance/{memory_id}")

    # ════════════════════════════════════════════════════════════════════════
    # Group 9: Storage / TTL
    # ════════════════════════════════════════════════════════════════════════
    def storage_health(self) -> dict: return self._get("/storage/health")
    def ttl_archive(self, chunk_id: str) -> dict:
        return self._post("/ttl/archive", params={"chunk_id": chunk_id})
    def ttl_archived(self, limit: int = 50) -> dict:
        return self._get("/ttl/archived", params={"limit": limit})

    # ════════════════════════════════════════════════════════════════════════
    # Group 10: Taxonomy & hierarchy
    # ════════════════════════════════════════════════════════════════════════
    def taxonomy_relations(self) -> dict: return self._get("/taxonomy/relations")
    def taxonomy_ancestors(self, child_tag: str) -> dict:
        return self._get(f"/taxonomy/ancestors/{child_tag}")
    def taxonomy_delete(self, child_tag: str) -> dict:
        return self._delete(f"/taxonomy/relations/{child_tag}")

    def hierarchy_create(self, *, label: str = "", parent_id: str | None = None,
                         type: str = "category", metadata: dict | None = None) -> dict:
        body: dict[str, Any] = {"label": label, "parent_id": parent_id,
                                "type": type, "metadata": metadata or {}}
        return self._post("/hierarchy", json=body)

    def hierarchy_get(self, node_id: str) -> dict:
        return self._get(f"/hierarchy/{node_id}")
    def hierarchy_tree(self, node_id: str) -> dict:
        return self._get(f"/hierarchy/{node_id}/tree")
    def hierarchy_enrich(self, chunk_id: str) -> dict:
        return self._get(f"/hierarchy/enrich/{chunk_id}")

    # ════════════════════════════════════════════════════════════════════════
    # Group 11: Episodes / core / procedural
    # ════════════════════════════════════════════════════════════════════════
    def core_get(self, user_id: str) -> dict: return self._get(f"/core/{user_id}")
    def core_list(self) -> dict: return self._get("/core")
    def core_upsert(self, user_id: str, profile: dict, source: str = "user") -> dict:
        return self._post(f"/core/{user_id}", json=profile, params={"source": source})

    def episode_create(self, *, session_id: str, title: str = "", summary: str = "",
                       user_id: str | None = None, duration_seconds: int = 0) -> dict:
        return self._post("/episodes", json={"session_id": session_id, "title": title,
                                              "summary": summary, "user_id": user_id,
                                              "duration_seconds": duration_seconds})

    def episode_get(self, memory_id_or_session_id: str) -> dict:
        return self._get(f"/episodes/{memory_id_or_session_id}")

    def procedural_create(self, *, name: str, description: str = "",
                          trigger: str = "", action: str = "") -> dict:
        return self._post("/procedural", json={"name": name, "description": description,
                                               "trigger": trigger, "action": action})

    def procedural_search(self, q: str = "", trigger: str = "", limit: int = 5) -> dict:
        kw: dict[str, Any] = {"limit": limit}
        if q: kw["q"] = q
        if trigger: kw["trigger"] = trigger
        return self._get("/procedural/search", params=kw)

    def procedural_outcome(self, proc_id: str, success: bool = True) -> dict:
        return self._post(f"/procedural/{proc_id}/outcome", params={"success": success})

    # ════════════════════════════════════════════════════════════════════════
    # Group 12: Media & resources
    # ════════════════════════════════════════════════════════════════════════
    def media_create(self, *, media_type: str, content: str,
                     mime_type: str = "text/plain", metadata: dict | None = None) -> dict:
        return self._post("/media", json={"media_type": media_type, "content": content,
                                          "mime_type": mime_type, "metadata": metadata or {}})

    def media_search(self, q: str, limit: int = 5) -> dict:
        return self._get("/media/search", params={"q": q, "limit": limit})
    def media_by_type(self, media_type: str, limit: int = 50) -> dict:
        return self._get(f"/media/by-type/{media_type}", params={"limit": limit})

    def resource_create(self, *, resource_type: str, title: str, content: str,
                        metadata: dict | None = None) -> dict:
        return self._post("/resources", json={"resource_type": resource_type, "title": title,
                                              "content": content, "metadata": metadata or {}})

    def resource_search(self, q: str, limit: int = 5) -> dict:
        return self._get("/resources/search", params={"q": q, "limit": limit})
    def resource_accessed(self, resource_id: str) -> dict:
        return self._post(f"/resources/{resource_id}/accessed")

    # ════════════════════════════════════════════════════════════════════════
    # Group 13: Consolidation & rumination
    # ════════════════════════════════════════════════════════════════════════
    def consolidate_extract(self, *, content: str, source_chunk_id: str | None = None,
                            user_id: str | None = None) -> dict:
        body: dict[str, Any] = {"content": content}
        if source_chunk_id: body["source_chunk_id"] = source_chunk_id
        if user_id: body["user_id"] = user_id
        return self._post("/consolidate/extract", json=body)

    def consolidate_run(self, *, user_id: str | None = None,
                        min_confidence: float = 0.5) -> dict:
        body: dict[str, Any] = {"min_confidence": min_confidence}
        if user_id: body["user_id"] = user_id
        return self._post("/consolidate/run", json=body)

    def consolidate_facts(self) -> dict:
        return self._get("/consolidate/facts")

    def rumination_stats(self) -> dict: return self._get("/rumination/stats")
    def rumination_run(self, *, lookback_days: int = 3, confirm: bool = False) -> dict:
        """POST /rumination/run — mutates metadata on contradicted chunks."""
        if not confirm:
            raise PermissionError("mutates chunk metadata; pass confirm=True")
        return self._post("/rumination/run", json={"lookback_days": lookback_days},
                          timeout=LONG_TIMEOUT)
    def rumination_abstract(self) -> dict:
        return self._post("/rumination/abstract-heuristics", json={})

    # ════════════════════════════════════════════════════════════════════════
    # Group 14: Conflict / reconsolidation / merge / reindex
    # ════════════════════════════════════════════════════════════════════════
    def conflict_upsert(self, **payload) -> dict:
        return self._post("/conflict/upsert", json=payload)
    def conflict_scan(self, **payload) -> dict:
        return self._post("/conflict/scan", json=payload)
    def conflict_log(self) -> dict: return self._get("/conflict/log")

    def reconsolidate(self, confirm: bool = False) -> dict:
        """POST /reconsolidate — decays 13759 relations × 0.95 + upgrades 500. MUTATES."""
        if not confirm:
            raise PermissionError("destructive: decays all relations; pass confirm=True")
        return self._post("/reconsolidate", timeout=LONG_TIMEOUT)

    def merge_duplicates(self, similarity_threshold: float = 0.9, confirm: bool = False) -> dict:
        """POST /merge-duplicates — adds similar relations. MUTATES."""
        if not confirm:
            raise PermissionError("mutates; pass confirm=True")
        return self._post("/merge-duplicates", params={"similarity_threshold": similarity_threshold},
                          timeout=LONG_TIMEOUT)
    def reindex(self) -> dict: return self._post("/reindex")

    # ════════════════════════════════════════════════════════════════════════
    # Group 15: Compress & cache
    # ════════════════════════════════════════════════════════════════════════
    def compress_run(self, *, max_importance: int = 4, max_per_batch: int = 50,
                     confirm: bool = False) -> dict:
        """POST /compress/run — inserts compressed summaries. MUTATES."""
        if not confirm:
            raise PermissionError("mutates; pass confirm=True")
        return self._post("/compress/run", params={"max_importance": max_importance,
                                                   "max_per_batch": max_per_batch})
    def compress_get(self, source_chunk_id: str) -> dict:
        return self._get(f"/compress/{source_chunk_id}")
    def pr_cache_stats(self) -> dict: return self._get("/cache/pr/stats")
    def pr_cache_clear(self) -> dict: return self._post("/cache/pr/clear")

    # ════════════════════════════════════════════════════════════════════════
    # Group 16: Access stats & refresh
    # ════════════════════════════════════════════════════════════════════════
    def access_stats(self, *, limit: int = 50, memory_id: str | None = None,
                     access_type: str | None = None, since: str | None = None) -> dict:
        kw: dict[str, Any] = {"limit": limit}
        if memory_id: kw["memory_id"] = memory_id
        if access_type: kw["access_type"] = access_type
        if since: kw["since"] = since
        return self._get("/access-stats", params=kw)
    def access_stats_for(self, memory_id: str) -> dict:
        return self._get(f"/access-stats/{memory_id}")
    def refresh_memory(self, memory_id: str) -> dict:
        return self._post(f"/memories/{memory_id}/refresh")

    # ════════════════════════════════════════════════════════════════════════
    # Group 17: Bench & eval
    # ════════════════════════════════════════════════════════════════════════
    def bench_multi_hop(self) -> dict: return self._get("/bench/multi-hop")
    def bench_custom(self, **payload) -> dict:
        return self._post("/bench/multi-hop/custom", json=payload)

    def eval_feedback(self, feedback: list[dict]) -> dict:
        """POST /eval/feedback — feedback items: {memory_id, evaluation_score, relevance_increment, judge_method}."""
        return self._post("/eval/feedback", json={"feedback": feedback})

    # ════════════════════════════════════════════════════════════════════════
    # Group 18: Admin / maintenance
    # ════════════════════════════════════════════════════════════════════════
    def admin_deprecate_by_type(self, type: str, *, dry_run: bool = True,
                                reason: str = "manual deprecation") -> dict:
        """POST /admin/deprecate-by-type — soft-deprecates chunks of a type."""
        return self._post("/admin/deprecate-by-type",
                          json={"type": type, "dry_run": dry_run, "reason": reason})

    def maintenance_stats(self) -> dict: return self._get("/admin/maintenance/stats")
    def maintenance_dead(self, limit: int = 100) -> dict:
        return self._get("/admin/maintenance/dead", params={"limit": limit})
    def maintenance_run_once(self) -> dict:
        """POST /admin/maintenance/run-once — drains one queue item synchronously."""
        return self._post("/admin/maintenance/run-once", timeout=LONG_TIMEOUT)
    def maintenance_enqueue(self, chunk_id: str, *, force: bool = False) -> dict:
        return self._post(f"/admin/maintenance/enqueue/{chunk_id}", params={"force": force})
    def maintenance_flush(self) -> dict:
        return self._post("/admin/maintenance/flush")

    def storage_evict(self, *, max_count: int = 50000, ratio: float = 0.05,
                      confirm: bool = False) -> dict:
        """POST /storage/evict — DELETES low-relevance chunks. Very destructive."""
        if not confirm:
            raise PermissionError("DESTRUCTIVE: deletes chunks; pass confirm=True")
        return self._post("/storage/evict", params={"max_count": max_count, "ratio": ratio},
                          timeout=LONG_TIMEOUT)
    def storage_enforce(self, max_size_mb: int = 1024) -> dict:
        return self._post("/storage/enforce", params={"max_size_mb": max_size_mb})
    def chroma_cleanup(self) -> dict: return self._get("/chroma/cleanup")

    # ════════════════════════════════════════════════════════════════════════
    # Group 19: Dashboard
    # ════════════════════════════════════════════════════════════════════════
    def dashboard(self) -> dict: return self._get("/dashboard")
    def dashboard_html(self) -> str:
        """GET /dashboard/html — returns the static HTML UI."""
        r = self.s.get(f"{self.base}/dashboard/html", timeout=self.timeout)
        r.raise_for_status()
        return r.text
    def root(self) -> str:
        """GET / — alias to dashboard HTML."""
        r = self.s.get(f"{self.base}/", timeout=self.timeout)
        r.raise_for_status()
        return r.text

    # ════════════════════════════════════════════════════════════════════════
    # Self-test
    # ════════════════════════════════════════════════════════════════════════
    def run_claude_smoke(self) -> dict:
        """Touch every endpoint group with a READ-ONLY or test-marker write.

        Safe to run repeatedly; it writes small 'claude-smoke' markers, then
        either cleans up via DELETE or tags them with `test` type so they
        don't pollute production recall."""
        import time
        results: dict[str, Any] = {"ok": [], "warn": [], "fail": []}
        T = lambda k, fn: self._safe(k, fn, results)
        marker = f"claude-smoke-{int(time.time())}"

        T("health",          lambda: self.health())
        T("stats",           lambda: self.stats())
        T("stats/by-type",   lambda: self.stats_by_type())
        T("stats/by-source", lambda: self.stats_by_source())
        T("stats/by-status", lambda: self.stats_by_status())
        T("stats/ops",       lambda: self.stats_ops())
        T("stats/embed-cache", lambda: self.stats_embed_cache())
        T("stats/health",    lambda: self.stats_health())
        T("endpoints",       lambda: self.list_endpoints())
        T("recommend",       lambda: self.recommend("connector_memory"))
        T("mode",            lambda: self.mode())

        cid = self.remember(marker, type="test", tags=["smoke", "claude-code"], importance=2)["id"]
        T("remember",        lambda: cid)
        T("list",            lambda: self.list(tags="smoke", limit=3))
        T("recall",          lambda: self.recall(marker, tags=["smoke"], limit=2))
        T("recall_tts",      lambda: self.recall_tts(marker))
        T("recall_hybrid",   lambda: self.recall_hybrid(marker))
        T("get_chunk",       lambda: self.get_chunk(cid, include_embedding=False))
        T("patch_chunk",     lambda: self.patch_chunk(cid, importance=3))
        T("update_chunk",    lambda: self.update_chunk(cid, marker, importance=2))
        T("forget",          lambda: self.forget(cid))
        T("batch",           lambda: self.remember_many([
            {"content": marker + "-A", "type": "test", "tags": ["smoke"], "importance": 2},
            {"content": marker + "-B", "type": "test", "tags": ["smoke"], "importance": 2},
        ]))
        T("list_after",      lambda: self.list(tags="smoke", limit=5))
        T("ttl_archived",    lambda: self.ttl_archived(limit=2))
        T("storage_health",  lambda: self.storage_health())
        T("ttl_archive",     lambda: self.ttl_archive(cid))  # will 404 since cid deleted
        T("forgetting_stats",    lambda: self.forgetting_stats())
        T("forgetting_expiring", lambda: self.forgetting_expiring_soon(days=30, limit=2))
        T("audit",           lambda: self.audit(limit=2))
        T("audit_log",       lambda: self.audit_log(action="claude-smoke",
                                                    memory_id=cid, actor="claude-code"))
        T("history",         lambda: self.history(cid))
        T("provenance",      lambda: self.provenance(cid))
        T("graph_sample",    lambda: self.graph_sample(limit=3))
        T("relations_batch", lambda: self.relations_batch([cid], depth=1, max_per_node=3))
        T("temporal_rel",    lambda: self.create_temporal_relation(cid, cid,
                                                                    relation_type="smoke"))
        T("temporal_get",    lambda: self.temporal_relations(cid))
        T("link_create",     lambda: self.link_create(cid, cid, link_type="smoke"))
        T("links_for",       lambda: self.links_for(cid))
        T("link_access",     lambda: self.link_access(cid, cid))
        T("link_path",       lambda: self.link_path(cid, cid))
        SID = "claude-smoke-session"
        T("working_set",     lambda: self.working_set(SID, "k", {"x": 1}))
        T("working_append_kv", lambda: self.working_append(SID, key="k", value={"y": 2}))
        T("working_append_rc", lambda: self.working_append(SID, role="user", content="hi"))
        T("working_get",     lambda: self.working_get(SID))
        T("working_get_key", lambda: self.working_get_key(SID, "k"))
        T("working_context", lambda: self.working_context(SID))
        T("hierarchy_create", lambda: self.hierarchy_create(label="claude-smoke", metadata={"chunk_id": cid}))
        T("taxonomy_rel",    lambda: self.taxonomy_relations())
        T("core_upsert",     lambda: self.core_upsert("claude-smoke-user", {"role": "tester"}))
        T("core_get",        lambda: self.core_get("claude-smoke-user"))
        T("episode_create",  lambda: self.episode_create(session_id=SID, summary="claude smoke"))
        T("episode_get",     lambda: self.episode_get(SID))
        T("proc_create",     lambda: self.procedural_create(name="claude-smoke", description="smoke"))
        T("proc_search",     lambda: self.procedural_search(q="smoke"))
        T("proc_outcome",    lambda: self.procedural_outcome("placeholder", success=True))
        T("media_create",    lambda: self.media_create(media_type="text", content=marker))
        T("media_search",    lambda: self.media_search(marker))
        T("media_by_type",   lambda: self.media_by_type("text"))
        T("res_create",      lambda: self.resource_create(resource_type="doc", title=marker, content="body"))
        T("res_search",      lambda: self.resource_search(marker))
        T("consolidate_extract", lambda: self.consolidate_extract(content=marker, source_chunk_id=cid))
        T("consolidate_facts",    lambda: self.consolidate_facts())
        T("rumination_stats",     lambda: self.rumination_stats())
        T("rumination_abstract",  lambda: self.rumination_abstract())
        T("bench_multi_hop",      lambda: self.bench_multi_hop())
        T("bench_custom",         lambda: self.bench_custom())
        T("reindex",              lambda: self.reindex())
        T("compress_get",         lambda: self.compress_get(cid))
        T("pr_cache_stats",       lambda: self.pr_cache_stats())
        T("pr_cache_clear",       lambda: self.pr_cache_clear())
        T("access_stats",         lambda: self.access_stats(limit=2))
        T("refresh_memory",       lambda: self.refresh_memory(cid))
        T("admin_deprecate",      lambda: self.admin_deprecate_by_type("test", dry_run=True))
        T("maintenance_stats",    lambda: self.maintenance_stats())
        T("maintenance_dead",     lambda: self.maintenance_dead(limit=3))
        T("maintenance_flush",    lambda: self.maintenance_flush())
        T("maintenance_enqueue",  lambda: self.maintenance_enqueue(cid))
        T("maintenance_run_once", lambda: self.maintenance_run_once())
        T("chroma_cleanup",       lambda: self.chroma_cleanup())
        T("storage_enforce",      lambda: self.storage_enforce(1024))
        T("dashboard",            lambda: self.dashboard())
        return results

    @staticmethod
    def _safe(name: str, fn, results: dict):
        try:
            r = fn()
            results["ok"].append(name)
            return r
        except CortexError as e:
            results["warn"].append((name, str(e)[:120]))
            return None
        except Exception as e:  # noqa: BLE001
            results["fail"].append((name, f"{type(e).__name__}: {str(e)[:120]}"))
            return None


if __name__ == "__main__":
    cx = Cortex()
    h = cx.health()
    print(f"[health] v={h.get('version')} chunks={h.get('chunks')} relations={h.get('relations')}")
    print("\n--- full self-test ---")
    r = cx.run_claude_smoke()
    print(f"OK   ({len(r['ok'])}): {r['ok']}")
    if r['warn']:
        print(f"WARN ({len(r['warn'])}):")
        for n, msg in r['warn']:
            print(f"  - {n}: {msg}")
    if r['fail']:
        print(f"FAIL ({len(r['fail'])}):")
        for n, msg in r['fail']:
            print(f"  - {n}: {msg}")