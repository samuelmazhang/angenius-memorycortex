"""
Memory Cortex Python client (opencode connector edition).

精简版 (V1.0, 适配 opencode 集成), 仅暴露 5 个主入口:
  remember / recall / forget / reason / introspect

底座: stdlib + urllib, zero-deps.

Usage:
    from connector.opencode.cortex_client import MemoryCortexClient
    cortex = MemoryCortexClient("http://127.0.0.1:1880")
    chunk = cortex.remember("I prefer Python for backend")
    results = cortex.recall("backend preference", top_k=3)

兼容性: 与 server/src/sdk/cortex_client.py (V5.4) 兼容; 删除了
高级方法 (list_chunks / save_procedure / 等), 由 memory_tool.py
facade 在此基础上扩展。
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from typing import List, Dict, Optional


class CortexError(Exception):
    """统一异常 (T2 在 memory_tool.py 中复用)."""

    def __init__(self, message: str, status_code: int = None, body: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class MemoryCortexClient:
    """同步 HTTP client, 5 个主入口."""

    DEFAULT_BASE_URL = "http://localhost:1880"

    def __init__(self, base_url: str = None, timeout: int = 30):
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # ---------- low-level HTTP ----------

    def _request(self, method: str, path: str,
                 params: Dict = None, body: Dict = None):
        """统一 HTTP 请求. None params 被 strip (避免 server 把 string 'None'
        当作 filter value). HTTPError 不 raise, 返回 error dict."""
        url = f"{self.base_url}{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            if params:
                qs = urllib.parse.urlencode(params)
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{qs}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if "application/json" in ct else raw
        except urllib.error.HTTPError as e:
            return {
                "error": str(e),
                "status": e.code,
                "body": e.read().decode("utf-8", errors="ignore")[:300],
            }
        except Exception as e:
            return {"error": str(e)}

    # ---------- 5 主入口 ----------

    def remember(self, content: str,
                 type: str = "knowledge",
                 tags=None,
                 importance: int = 5,
                 session_id: str = None,
                 user_id: str = None,
                 metadata: Dict = None) -> Dict:
        """写入一段记忆. CoALA 自动决策 (ADD/UPDATE/MERGE/NOOP)."""
        # Coerce tags
        coerced_tags: List[str] = []
        if tags is not None:
            if isinstance(tags, str):
                coerced_tags = [t.strip() for t in tags.replace("\n", ",").split(",") if t.strip()]
            elif isinstance(tags, (list, tuple)):
                for t in tags:
                    if isinstance(t, str) and t.strip():
                        coerced_tags.append(t.strip())
                    elif t is not None:
                        coerced_tags.append(str(t))

        merged = dict(metadata or {})
        if user_id:
            merged["user_id"] = user_id

        payload: Dict = {
            "content": content,
            "type": type,
            "importance": importance,
        }
        if coerced_tags:
            payload["tags"] = coerced_tags
        if merged:
            payload["metadata"] = merged
        result = self._request("POST", "/chunks", body=payload)

        # Bonus: working memory
        if session_id and isinstance(result, dict) and "id" in result:
            self._request("POST", "/working/set", body={
                "session_id": session_id,
                "key": f"chunk_{result['id']}",
                "value": content[:200],
            })
        # Bonus: extract trigger (best-effort)
        if user_id and isinstance(result, dict) and "id" in result:
            try:
                self._request("POST", "/consolidate/extract", body={
                    "chunk_id": result["id"],
                    "content": content,
                    "user_id": user_id,
                })
            except Exception:
                pass
        return result

    def recall(self, query: str,
               top_k: int = 5,
               include_context: bool = True,
               session_id: str = None,
               user_id: str = None):
        """召回到 query 相关的记忆. user_id 在客户端按 metadata.user_id 过滤."""
        fetch_k = int(top_k) * 4 if user_id else int(top_k)
        result = self._request("POST", "/search", body={
            "query": query,
            "limit": fetch_k,
        })
        if isinstance(result, dict):
            hits = result.get("results", result)
        else:
            hits = result

        if user_id and isinstance(hits, list):
            hits = [h for h in hits
                    if (h.get("metadata") or {}).get("user_id") == user_id][:int(top_k)]

        if include_context and session_id:
            ctx = self._request("GET", f"/working/{session_id}/context")
            if isinstance(ctx, dict):
                if isinstance(hits, list):
                    hits = {"results": hits, "context": {
                        "working_memory": ctx.get("working", []),
                        "conversation": ctx.get("conversation", []),
                    }}
                else:
                    hits["context"] = {
                        "working_memory": ctx.get("working", []),
                        "conversation": ctx.get("conversation", []),
                    }
        return hits

    def forget(self, chunk_id: str = None, tag: str = None,
               expire_old_days: int = None,
               user_id: str = None) -> Dict:
        """删除/归档记忆."""
        if chunk_id:
            r = self._request("DELETE", f"/chunks/{chunk_id}")
            return {"action": "delete", "chunk_id": chunk_id, "result": r}
        if expire_old_days is not None:
            r = self._request("POST", "/ttl/archive", body={})
            return {"action": "ttl_archive", "result": r}
        if tag or user_id:
            chunks = self._request("POST", "/list", body={
                "tags": [tag] if tag else None,
                "limit": 100,
            })
            deleted = 0
            if isinstance(chunks, list):
                for c in chunks:
                    if not user_id or c.get("user_id") == user_id:
                        r = self._request("DELETE", f"/chunks/{c['id']}")
                        if "error" not in r:
                            deleted += 1
            return {"action": "bulk_delete", "count": deleted}
        return {"error": "must specify chunk_id, tag, user_id, or expire_old_days"}

    def reason(self, question: str,
               top_k: int = 5,
               include_hierarchy: bool = True,
               include_links: bool = True) -> Dict:
        """RAG-style: 召回 + hierarchy + links + LLM-ready context."""
        # 1. Hybrid + regular search (RRF-style merge)
        hybrid = self._request("POST", "/search/hybrid", body={
            "query": question, "top_k": top_k,
        })
        regular = self._request("POST", "/search", body={
            "query": question, "limit": top_k,
        })
        chunks = []
        if isinstance(regular, dict):
            chunks = regular.get("results", regular)
        # Merge: dedupe by id, prefer order from hybrid vector then regular
        rankings = {"vector": [], "fts5": []}
        if isinstance(hybrid, dict):
            ranks = hybrid.get("rankings_by_strategy", {})
            rankings = ranks or rankings
        for cid in rankings.get("vector", []):
            if cid not in rankings["fts5"]:
                rankings["fts5"].append(cid)
        for c in chunks:
            cid = c.get("id")
            if cid and cid not in rankings["fts5"]:
                rankings["fts5"].append(cid)

        # 2. Hierarchy enrich
        hierarchies = []
        if include_hierarchy:
            for c in chunks[:top_k]:
                cid = c.get("id")
                if cid:
                    h = self._request("GET", f"/hierarchy/enrich/{cid}")
                    if isinstance(h, dict) and h.get("hierarchy_context"):
                        hierarchies.append({"chunk_id": cid, "context": h["hierarchy_context"]})

        # 3. Related links
        links_per_chunk = []
        if include_links:
            for c in chunks[:top_k]:
                cid = c.get("id")
                if cid:
                    ls = self._request("GET", f"/links/{cid}", params={"limit": 5})
                    if isinstance(ls, dict) and ls.get("links"):
                        links_per_chunk.append({"chunk_id": cid, "links": ls["links"][:5]})

        # 4. LLM-ready context string
        ctx_lines = [f"[{i}] {c.get('content', '')}" for i, c in enumerate(chunks, 1)]
        return {
            "question": question,
            "chunks": chunks[:top_k],
            "context_chunks": "\n".join(ctx_lines),
            "hierarchies": hierarchies,
            "links": links_per_chunk,
        }

    def introspect(self) -> Dict:
        """Self-aware metadata."""
        return {
            "health": self._request("GET", "/health"),
            "stats": self._request("GET", "/stats"),
            "ops_counters": self._request("GET", "/stats/ops"),
        }

    # ---------- low-level extras ----------

    def log_audit(self, action: str, resource_type: str, **kwargs) -> Dict:
        """记录 audit (G7 兼容)."""
        return self._request("POST", "/audit/log", body={
            "action": action,
            "resource_type": resource_type,
            **kwargs,
        })