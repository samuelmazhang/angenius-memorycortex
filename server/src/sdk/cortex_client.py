"""
Memory Cortex Python SDK — 让 agent 用 5 个主入口调用 cortex 全功能.

入口 (按 agent 思维):
  - remember(content, ...)   → 写入一段记忆 (CoALA 决策自动)
  - recall(query, ...)       → 召回到 query 相关的记忆
  - forget(chunk_id, ...)    → 删 / archive 一段记忆
  - reason(question, ...)    → 思考链 + 召回 context (RAG-style)
  - introspect()             → 返回 self-aware metadata (count, health, schema)

底层: 直接打 FastAPI HTTP endpoints (/search, /chunks, /working/...)
零 deps (只 stdlib + urllib), 无需 pip install.

Usage:
    from src.sdk.cortex_client import MemoryCortexClient

    cortex = MemoryCortexClient("http://localhost:1880")

    # 写入
    chunk = cortex.remember("I prefer Python for backend")
    print(f"  stored: {chunk['id']}")

    # 召回
    results = cortex.recall("backend preference", top_k=3)
    for r in results:
        print(f"  {r['id']}: {r['content'][:50]}")

    # 思考 (RAG-style)
    answer = cortex.reason("What's my backend preference?")
    print(f"  context: {answer['context_chunks']}")

    # forget
    cortex.forget(chunk['id'])
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from typing import List, Dict, Optional, Any
from datetime import datetime


class MemoryCortexClient:
    """Memory Cortex 同步 HTTP 客户端 (单文件, 零依赖)."""

    def __init__(self, base_url: str = "http://localhost:1880", timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    # ---------- low-level HTTP ----------

    def _request(self, method: str, path: str,
                 params: Dict = None, body: Dict = None) -> Any:
        """Centralized HTTP request helper.

        NOTE (2026-08-20, Cortex V5.4 conflict-log fix): we strip ``None``
        values from ``params`` before url-encoding. ``urllib.parse.urlencode``
        otherwise serializes Python ``None`` as the literal string ``"None"``,
        which the FastAPI backend then treats as a real filter value
        (``WHERE user_id = 'None'``) and returns zero rows. The async
        cousin in ``connector/MinimaxCode/src/cortex_client.py`` already
        does this; the sync SDK now matches.
        """
        url = f"{self.base_url}{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            if params:
                qs = urllib.parse.urlencode(params)
                url = f"{url}?{qs}" if '?' not in url else f"{url}&{qs}"
        data = None
        headers = {'Content-Type': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get('Content-Type', '')
                body_resp = resp.read().decode('utf-8')
                if 'application/json' in content_type:
                    return json.loads(body_resp)
                return body_resp
        except urllib.error.HTTPError as e:
            return {'error': str(e), 'status': e.code, 'body': e.read().decode('utf-8', errors='ignore')[:300]}
        except Exception as e:
            return {'error': str(e)}

    # ---------- 5 high-level 主入口 ----------

    def remember(self, content: str,
                 type: str = "knowledge",
                 tags: List[str] = None,
                 importance: int = 5,
                 session_id: str = None,
                 user_id: str = None,
                 metadata: Dict = None) -> Dict:
        """写入一段记忆. CoALA 决策自动触发 (ADD / UPDATE / MERGE / NOOP).

        Args:
            content: 记忆内容 (text)
            type: 'knowledge' | 'experience' | 'decision' | 'context' | 'preference' | ...
            tags: list of tags (free-form)
            importance: 1-10
            session_id: optional, log to working memory
            user_id: optional, persist into chunk.metadata.user_id so
                (a) cortex_recall(user_id=...) can filter,
                (b) conflict-resolve's `metadata->>'user_id'` bucket actually fires.
                Backward-compat: when omitted, chunk has no user_id (legacy shared pool).
            metadata: optional dict merged into chunk.metadata. If both
                user_id and metadata are passed, user_id wins on conflict.

        Returns:
            chunk dict {id, content, type, tags, importance, ...}
        """
        # Merge user_id into metadata. We persist it as `metadata.user_id`
        # because:
        # 1) The memory_chunks table has no user_id column — it lives in the
        #    JSONB `metadata` field (see server/src/api_pg.py chunk insert).
        # 2) conflict-resolve queries `metadata->>'user_id'` for bucket
        #    routing (api_pg.py:2585-2600) — without writing user_id here,
        #    the bucket is dead code.
        # 3) cortex_recall(user_id=...) does client-side filtering against
        #    `chunk.metadata.user_id` — see recall() below.
        merged_metadata: Dict = dict(metadata or {})
        if user_id:
            merged_metadata['user_id'] = user_id

        # Coerce tags into a clean list[str]. Accept list/tuple/None/comma-
        # separated string so non-array JSON Schema clients (some MCP
        # transports degrade ``list`` to ``string``) don't 422 at the
        # Pydantic boundary in api_pg.py.
        coerced_tags: List[str] = []
        if tags is not None:
            if isinstance(tags, str):
                coerced_tags = [t.strip() for t in tags.replace("\n", ",").split(",") if t.strip()]
            elif isinstance(tags, (list, tuple)):
                for t in tags:
                    if isinstance(t, str):
                        t = t.strip()
                        if t:
                            coerced_tags.append(t)
                    elif t is not None:
                        coerced_tags.append(str(t))
            else:
                coerced_tags = [str(tags)]

        payload: Dict = {
            'content': content,
            'type': type,
            'importance': importance,
        }
        if coerced_tags:
            payload['tags'] = coerced_tags
        if merged_metadata:
            payload['metadata'] = merged_metadata
        result = self._request('POST', '/chunks', body=payload)

        # Bonus: save to working memory if session_id
        if session_id and 'id' in result:
            self._request('POST', '/working/set', body={
                'session_id': session_id,
                'key': f"chunk_{result['id']}",
                'value': content[:200],
            })

        # Bonus: extraction trigger for G2 (consolidation)
        if user_id and 'id' in result:
            try:
                self._request('POST', '/consolidate/extract', body={
                    'chunk_id': result['id'],
                    'content': content,
                    'user_id': user_id,
                })
            except Exception:
                pass

        return result

    def recall(self, query: str,
               top_k: int = 5,
               strategies: str = "vector+fts5",
               include_context: bool = True,
               session_id: str = None,
               user_id: str = None) -> List[Dict]:
        """召回到 query 相关的记忆.

        Args:
            query: search text
            top_k: how many results (default 5)
            strategies: 'vector+fts5' (hybrid) | 'vector' | 'fts5' | 'hybrid' (RRF)
            include_context: 加 working memory context
            session_id: 可选, 拉 working memory + turn context
            user_id: 可选, 按 chunk.metadata.user_id 客户端过滤.
                后端 /search 当前不支持 user_id 维度 (api_pg.py /search
                只看 vector + FTS5), 所以我们在 recall 层 over-fetch 然后
                过滤, 保证 top_k 个结果都属于 user_id.
                Backward-compat: None = 共享池 (legacy 行为).

        Returns:
            list of chunk dicts [{id, content, similarity, tags, type, ...}]
        """
        # Over-fetch when filtering by user_id, to keep top_k after filter
        # (the per-user subset is typically a small fraction of all hits).
        fetch_k = int(top_k) * 4 if user_id else int(top_k)
        result = self._request('POST', '/search', body={
            'query': query,
            'limit': fetch_k,
        })

        # Normalize: FastAPI response is {total, results}
        if isinstance(result, dict):
            hits = result.get('results', result)
        else:
            hits = result

        # Client-side user_id filter against chunk.metadata.user_id
        # (memory_chunks 表本身无 user_id 列, 见 api_pg.py chunk 表 schema).
        if user_id and isinstance(hits, list):
            hits = [
                h for h in hits
                if (h.get('metadata') or {}).get('user_id') == user_id
            ][:int(top_k)]
        elif user_id and isinstance(hits, dict):
            # 防御: 万一 hits 是 dict 包装结构
            inner = hits.get('results', [])
            hits['results'] = [
                h for h in inner
                if (h.get('metadata') or {}).get('user_id') == user_id
            ][:int(top_k)]

        # Optional context (working memory)
        context = None
        if include_context and session_id:
            context_result = self._request('GET', f'/working/{session_id}/context')
            if isinstance(context_result, dict):
                context = {
                    'working_memory': context_result.get('working', []),
                    'conversation': context_result.get('conversation', []),
                }

        if context is not None:
            if isinstance(hits, list):
                # Inject context into first item (for RAG agents)
                hits = {'results': hits, 'context': context}
            else:
                hits['context'] = context

        return hits

    def forget(self, chunk_id: str = None, tag: str = None,
               expire_old_days: int = None,
               user_id: str = None) -> Dict:
        """删除 / 归档记忆.

        Args:
            chunk_id: specific chunk id to delete
            tag: delete all chunks with this tag
            expire_old_days: archive chunks older than N days (TTL)
            user_id: delete all chunks for this user

        Returns:
            {action: 'delete' | 'archive', count: int}
        """
        if chunk_id:
            r = self._request('DELETE', f'/chunks/{chunk_id}')
            return {'action': 'delete', 'chunk_id': chunk_id, 'result': r}

        if expire_old_days is not None:
            r = self._request('POST', '/ttl/archive', body={})
            return {'action': 'ttl_archive', 'result': r}

        # Tag/user-based bulk delete (low-level fallback)
        if tag or user_id:
            # List chunks by tag/user then delete each
            chunks = self._request('POST', '/list', body={
                'tags': [tag] if tag else None,
                'limit': 100,
            })
            deleted = 0
            if isinstance(chunks, list):
                for c in chunks:
                    if not user_id or c.get('user_id') == user_id:
                        r = self._request('DELETE', f"/chunks/{c['id']}")
                        if 'error' not in r:
                            deleted += 1
            return {'action': 'bulk_delete', 'count': deleted}

        return {'error': 'must specify chunk_id, tag, user_id, or expire_old_days'}

    def reason(self, question: str,
               top_k: int = 5,
               include_hierarchy: bool = True,
               include_links: bool = True) -> Dict:
        """RAG-style: 召回到 question 相关的 chunks, 同时 enrich hierarchy + links context.

        Args:
            question: natural language question
            top_k: how many top results to include
            include_hierarchy: 加 chunk → step → episode → session 上下文
            include_links: 加 Zettelkasten related links

        Returns:
            {
              'question': str,
              'chunks': [...top-k chunks],
              'context_chunks': [str, ...] (ready for LLM prompt),
              'hierarchies': [...chunk → session chain],
              'links': [...related chunks]
            }
        """
        # 1. Hybrid search
        search_result = self._request('POST', '/search/hybrid', body={
            'query': question,
            'top_k': top_k,
        })
        # Combine with regular search
        regular = self._request('POST', '/search', body={
            'query': question,
            'limit': top_k,
        })
        chunks = []
        if isinstance(regular, dict):
            chunks = regular.get('results', regular)
        # RRF
        rankings = {'vector': [], 'fts5': []}
        if isinstance(search_result, dict):
            ranks = search_result.get('rankings_by_strategy', {})
            rankings = ranks or rankings
        for r in rankings.get('vector', []):
            if r not in rankings['fts5']:
                rankings['fts5'].append(r)
        for c in chunks:
            cid = c.get('id')
            if cid and cid not in rankings['fts5']:
                rankings['fts5'].append(cid)

        # 2. Enrich with hierarchy
        hierarchies = []
        if include_hierarchy:
            for c in chunks[:top_k]:
                cid = c.get('id')
                if cid:
                    h = self._request('GET', f'/hierarchy/enrich/{cid}')
                    if isinstance(h, dict) and 'hierarchy_context' in h:
                        ctx = h.get('hierarchy_context')
                        if ctx:
                            hierarchies.append({'chunk_id': cid, 'context': ctx})

        # 3. Find related links
        links_per_chunk = []
        if include_links:
            for c in chunks[:top_k]:
                cid = c.get('id')
                if cid:
                    ls = self._request('GET', f'/links/{cid}', params={'limit': 5})
                    if isinstance(ls, dict) and ls.get('links'):
                        links_per_chunk.append({'chunk_id': cid, 'links': ls['links'][:5]})

        # 4. Build context string for LLM prompt
        context_lines = []
        for i, c in enumerate(chunks, 1):
            context_lines.append(f"[{i}] {c.get('content', '')}")
        context_chunks = '\n'.join(context_lines)

        return {
            'question': question,
            'chunks': chunks[:top_k],
            'context_chunks': context_chunks,
            'hierarchies': hierarchies,
            'links': links_per_chunk,
        }

    def introspect(self) -> Dict:
        """Self-aware metadata: total chunks, health, schema version, ops counters.

        Returns:
            dict with health, version, totals, schema, ops_counters
        """
        health = self._request('GET', '/health')
        stats = self._request('GET', '/stats')
        ops = self._request('GET', '/stats/ops')
        return {
            'health': health,
            'stats': stats,
            'ops_counters': ops,
        }

    # ---------- 额外便捷方法 ----------

    def log_audit(self, action: str, resource_type: str, **kwargs):
        """记录 access audit (G7)."""
        return self._request('POST', '/audit/log', body={
            'action': action,
            'resource_type': resource_type,
            **kwargs,
        })

    def list_chunks(self, tags: List[str] = None, type: str = None,
                    limit: int = 20) -> List[Dict]:
        """List chunks (filter by tags/type)."""
        payload = {'limit': limit}
        if tags:
            payload['tags'] = tags
        if type:
            payload['type'] = type
        return self._request('POST', '/list', body=payload)

    def get_chunk(self, chunk_id: str) -> Dict:
        return self._request('GET', f'/chunks/{chunk_id}')

    def update_chunk(self, chunk_id: str, content: str = None,
                     importance: int = None) -> Dict:
        payload = {}
        if content:
            payload['content'] = content
        if importance:
            payload['importance'] = importance
        return self._request('POST', f'/chunks/{chunk_id}', body=payload)

    def get_relations(self, chunk_id: str) -> Dict:
        return self._request('GET', f'/relations/{chunk_id}')

    def create_temporal_relation(self, source_id: str, target_id: str,
                                  relation_type: str = 'related',
                                  weight: float = 0.5) -> Dict:
        """G1+D1: create time-aware relationship."""
        return self._request('POST', '/relations/temporal', body={
            'source_id': source_id,
            'target_id': target_id,
            'relation_type': relation_type,
            'weight': weight,
        })

    def create_link(self, source_id: str, target_id: str,
                    link_type: str = 'related', strength: float = 0.5) -> Dict:
        """G3: create Zettelkasten link."""
        return self._request('POST', '/links/create', body={
            'source_id': source_id,
            'target_id': target_id,
            'link_type': link_type,
            'strength': strength,
        })

    def build_context(self, session_id: str) -> Dict:
        """G1: build LLM context from working memory + conversation."""
        return self._request('GET', f'/working/{session_id}/context')

    def set_working_memory(self, session_id: str, key: str, value: str,
                           value_type: str = 'text') -> Dict:
        return self._request('POST', '/working/set', body={
            'session_id': session_id,
            'key': key,
            'value': value,
            'value_type': value_type,
        })

    def consolidate_facts(self, user_id: str, min_confidence: float = 0.5) -> Dict:
        """G2: apply extracted facts to user core profile."""
        return self._request('POST', '/consolidate/run', body={
            'user_id': user_id,
            'min_confidence': min_confidence,
        })

    def benchmark_multi_hop(self, queries: List[Dict] = None) -> Dict:
        """D4: run multi-hop benchmark."""
        if queries:
            return self._request('POST', '/bench/multi-hop/custom', body={'queries': queries})
        return self._request('GET', '/bench/multi-hop')



    # ============== H3 (2026-07-27 V4.4): SDK 扩展 — 覆盖全 103 endpoints ==============

    # Hierarchy
    def create_hierarchy_node(self, level: str, title: str,
                              parent_id: str = None,
                              memory_chunk_id: str = None) -> Dict:
        return self._request('POST', '/hierarchy', body={
            'level': level, 'title': title,
            'parent_id': parent_id, 'memory_chunk_id': memory_chunk_id,
        })

    def get_hierarchy_tree(self, root_id: str, max_depth: int = 5) -> Dict:
        return self._request('GET', f'/hierarchy/{root_id}/tree',
                             params={'max_depth': max_depth})

    # Episodes (CFGM)
    def remember_episode(self, session_id: str, title: str, summary: str,
                         parent_chunk_id: str = None) -> Dict:
        return self._request('POST', '/episodes', body={
            'session_id': session_id, 'title': title, 'summary': summary,
            'parent_chunk_id': parent_chunk_id,
        })

    def get_episodes_for_memory(self, memory_id: str) -> List[Dict]:
        return self._request('GET', f'/episodes/{memory_id}').get('episodes', [])

    # Procedural (PlugMem)
    def learn_procedure(self, name: str, trigger: str, action: str,
                        description: str = '') -> Dict:
        return self._request('POST', '/procedural', body={
            'name': name, 'trigger': trigger, 'action': action,
            'description': description,
        })

    def find_procedure(self, trigger_query: str, limit: int = 5) -> List[Dict]:
        return self._request('GET', '/procedural/search',
                             params={'trigger': trigger_query, 'limit': limit}).get('procedures', [])

    def record_procedure_success(self, proc_id: str) -> bool:
        return self._request('POST', f'/procedural/{proc_id}/outcome',
                              body={'success': True}).get('status') == 'recorded'

    # Provenance + history (lineage)
    def get_provenance(self, memory_id: str) -> Dict:
        return self._request('GET', f'/provenance/{memory_id}')

    def get_history(self, memory_id: str) -> List[Dict]:
        return self._request('GET', f'/history/{memory_id}').get('history', [])

    # Relations
    def get_relations(self, memory_id: str) -> List[Dict]:
        return self._request('GET', f'/relations/{memory_id}').get('relations', [])

    def delete_relation(self, memory_id: str, target_id: str) -> bool:
        return self._request('DELETE', f'/relations/{memory_id}/{target_id}').get('status') == 'deleted'

    # Reconsolidation (CoALA + Zep)
    def reconsolidate(self, memory_id: str = None) -> int:
        return self._request('POST', '/reconsolidate',
                              body={'memory_id': memory_id}).get('affected', 0)

    def merge_duplicates(self, threshold: float = 0.85) -> int:
        return self._request('POST', '/merge-duplicates',
                              body={'similarity_threshold': threshold}).get('merged_count', 0)

    def refresh_memory(self, memory_id: str) -> bool:
        return self._request('POST', f'/memories/{memory_id}/refresh').get('status') == 'refreshed'

    # Forgetting
    def forgetting_stats(self) -> Dict:
        return self._request('GET', '/forgetting/stats')

    def cleanup_forgotten(self, min_score: float = 0.1) -> int:
        return self._request('POST', '/forgetting/cleanup',
                              body={'min_score': min_score}).get('expired_count', 0)

    def expiring_soon(self, days: int = 7) -> List[Dict]:
        return self._request('GET', '/forgetting/expiring-soon',
                              params={'days': days}).get('expiring', [])

    # Rumination
    def ruminate(self, min_cluster_size: int = 3) -> Dict:
        return self._request('POST', '/rumination/run',
                              body={'min_cluster_size': min_cluster_size})

    def abstract_heuristics(self, force: bool = False) -> int:
        return self._request('POST', '/rumination/abstract-heuristics',
                              body={'force': force}).get('abstracted', 0)

    # Storage
    def storage_health(self) -> Dict:
        return self._request('GET', '/storage/health')

    def enforce_storage(self, max_size_mb: int = 1024) -> int:
        return self._request('POST', '/storage/enforce',
                              body={'max_size_mb': max_size_mb}).get('evicted', 0)

    # Taxonomy
    def taxonomy_relations(self) -> List[Dict]:
        return self._request('GET', '/taxonomy/relations').get('relations', [])

    def taxonomy_ancestors(self, child_tag: str) -> List[str]:
        return self._request('GET', f'/taxonomy/ancestors/{child_tag}').get('ancestors', [])

    # Cache
    def pr_cache_stats(self) -> Dict:
        return self._request('GET', '/cache/pr/stats')

    def clear_pr_cache(self) -> bool:
        return self._request('POST', '/cache/pr/clear').get('status') == 'cleared'

    # Bench
    def bench_multi_hop(self, queries: List[Dict] = None) -> Dict:
        if queries:
            return self._request('POST', '/bench/multi-hop/custom', body={'queries': queries})
        return self._request('GET', '/bench/multi-hop')

    # Eval / feedback
    def submit_feedback(self, memory_id: str, feedback_type: str,
                        score: float, comment: str = '') -> bool:
        return self._request('POST', '/eval/feedback', body={
            'memory_id': memory_id, 'feedback_type': feedback_type,
            'score': score, 'comment': comment,
        }).get('status') == 'recorded'

    # Access stats
    def access_stats(self, memory_id: str = None) -> Dict:
        if memory_id:
            return self._request('GET', f'/access-stats/{memory_id}')
        return self._request('GET', '/access-stats')

    # High-level 综合主入口
    def deep_context(self, memory_id: str) -> Dict:
        """Comprehensive context for one memory: chunk + provenance + history + relations + hierarchy + links."""
        return {
            'chunk': self.get_chunk(memory_id),
            'provenance': self.get_provenance(memory_id),
            'history': self.get_history(memory_id),
            'relations': self.get_relations(memory_id),
            'hierarchy': self._request('GET', f'/hierarchy/enrich/{memory_id}'),
            'links': self._request('GET', f'/links/{memory_id}').get('links', []),
        }

    def agent_health(self) -> Dict:
        """All-in-one health check for agent startup."""
        try:
            return {
                'server': self._request('GET', '/health'),
                'stats': self._request('GET', '/stats'),
                'pr_cache': self.pr_cache_stats(),
                'storage': self.storage_health(),
                'forgetting': self.forgetting_stats(),
            }
        except Exception as e:
            return {'error': str(e)}


# ---------- 便捷 CLI helper ----------

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m src.sdk.cortex_client <command> [args]")
        print("Commands:")
        print("  introspect                      - show health + stats + counters")
        print("  remember <content> [tag1,tag2]   - write memory")
        print("  recall <query> [top_k]          - search memory")
        print("  reason <question>               - RAG-style reasoning")
        sys.exit(1)

    cmd = sys.argv[1]
    client = MemoryCortexClient()

    if cmd == 'introspect':
        import json
        d = client.introspect()
        print(json.dumps(d, indent=2, ensure_ascii=False))
    elif cmd == 'remember':
        content = sys.argv[2]
        tags = sys.argv[3].split(',') if len(sys.argv) > 3 else None
        result = client.remember(content, tags=tags or [])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == 'recall':
        query = sys.argv[2]
        top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        results = client.recall(query, top_k=top_k)
        if isinstance(results, list):
            for r in results:
                print(f"  - {r.get('id', '?')[:30]} {r.get('content', '')[:60]}")
        else:
            print(json.dumps(results, indent=2, ensure_ascii=False))
    elif cmd == 'reason':
        question = ' '.join(sys.argv[2:])
        r = client.reason(question)
        print(f"Question: {r['question']}")
        print(f"\nContext:\n{r['context_chunks'][:500]}")
        print(f"\nHierarchies: {len(r['hierarchies'])}")
        print(f"Links: {len(r['links'])}")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


# ============== H3 (2026-07-27): V4.4 SDK extensions — 覆盖全 103 endpoints ==============

# Hierarchy
def create_hierarchy_node(self, level: str, title: str, parent_id: str = None,
                          memory_chunk_id: str = None) -> Dict:
    return self._request('POST', '/hierarchy', body={
        'level': level, 'title': title,
        'parent_id': parent_id, 'memory_chunk_id': memory_chunk_id,
    })

def get_hierarchy_tree(self, root_id: str, max_depth: int = 5) -> Dict:
    return self._request('GET', f'/hierarchy/{root_id}/tree', params={'max_depth': max_depth})

# Episodes (CFGM)
def remember_episode(self, session_id: str, title: str, summary: str,
                     parent_chunk_id: str = None) -> Dict:
    return self._request('POST', '/episodes', body={
        'session_id': session_id, 'title': title, 'summary': summary,
        'parent_chunk_id': parent_chunk_id,
    })

# Procedural (PlugMem)
def learn_procedure(self, name: str, trigger: str, action: str,
                    description: str = '') -> Dict:
    return self._request('POST', '/procedural', body={
        'name': name, 'trigger': trigger, 'action': action,
        'description': description,
    })

def find_procedure(self, trigger_query: str, limit: int = 5) -> List[Dict]:
    return self._request('GET', '/procedural/search',
                         params={'trigger': trigger_query, 'limit': limit}).get('procedures', [])

def record_procedure_success(self, proc_id: str) -> bool:
    return self._request('POST', f'/procedural/{proc_id}/outcome',
                          body={'success': True}).get('status') == 'recorded'

# Provenance + history (lineage)
def get_provenance(self, memory_id: str) -> Dict:
    return self._request('GET', f'/provenance/{memory_id}')

def get_history(self, memory_id: str) -> List[Dict]:
    return self._request('GET', f'/history/{memory_id}').get('history', [])

# Reconsolidation (CoALA + Zep)
def reconsolidate(self, memory_id: str = None) -> int:
    return self._request('POST', '/reconsolidate', body={'memory_id': memory_id}).get('affected', 0)

def merge_duplicates(self, threshold: float = 0.85) -> int:
    return self._request('POST', '/merge-duplicates',
                          body={'similarity_threshold': threshold}).get('merged_count', 0)

def refresh_memory(self, memory_id: str) -> bool:
    return self._request('POST', f'/memories/{memory_id}/refresh').get('status') == 'refreshed'

# Forgetting
def forgetting_stats(self) -> Dict:
    return self._request('GET', '/forgetting/stats')

def cleanup_forgotten(self, min_score: float = 0.1) -> int:
    return self._request('POST', '/forgetting/cleanup',
                          body={'min_score': min_score}).get('expired_count', 0)

def expiring_soon(self, days: int = 7) -> List[Dict]:
    return self._request('GET', '/forgetting/expiring-soon',
                          params={'days': days}).get('expiring', [])

# Rumination
def ruminate(self, min_cluster_size: int = 3) -> Dict:
    return self._request('POST', '/rumination/run',
                          body={'min_cluster_size': min_cluster_size})

def abstract_heuristics(self, force: bool = False) -> int:
    return self._request('POST', '/rumination/abstract-heuristics',
                          body={'force': force}).get('abstracted', 0)

# Storage
def storage_health(self) -> Dict:
    return self._request('GET', '/storage/health')

def enforce_storage(self, max_size_mb: int = 1024) -> int:
    return self._request('POST', '/storage/enforce',
                          body={'max_size_mb': max_size_mb}).get('evicted', 0)

# Taxonomy
def taxonomy_relations(self) -> List[Dict]:
    return self._request('GET', '/taxonomy/relations').get('relations', [])

def taxonomy_ancestors(self, child_tag: str) -> List[str]:
    return self._request('GET', f'/taxonomy/ancestors/{child_tag}').get('ancestors', [])

# Cache
def pr_cache_stats(self) -> Dict:
    return self._request('GET', '/cache/pr/stats')

def clear_pr_cache(self) -> bool:
    return self._request('POST', '/cache/pr/clear').get('status') == 'cleared'

# Bench
def bench_multi_hop(self, queries: List[Dict] = None) -> Dict:
    if queries:
        return self._request('POST', '/bench/multi-hop/custom', body={'queries': queries})
    return self._request('GET', '/bench/multi-hop')

# Eval / feedback
def submit_feedback(self, memory_id: str, feedback_type: str,
                     score: float, comment: str = '') -> bool:
    return self._request('POST', '/eval/feedback', body={
        'memory_id': memory_id, 'feedback_type': feedback_type,
        'score': score, 'comment': comment,
    }).get('status') == 'recorded'

# Access stats
def access_stats(self, memory_id: str = None) -> Dict:
    if memory_id:
        return self._request('GET', f'/access-stats/{memory_id}')
    return self._request('GET', '/access-stats')

# Relations
def get_relations(self, memory_id: str) -> List[Dict]:
    return self._request('GET', f'/relations/{memory_id}').get('relations', [])

def delete_relation(self, memory_id: str, target_id: str) -> bool:
    return self._request('DELETE', f'/relations/{memory_id}/{target_id}').get('status') == 'deleted'

# High-level 综合主入口
def deep_context(self, memory_id: str) -> Dict:
    """Comprehensive context for one memory: chunk + provenance + history + relations + hierarchy + links."""
    return self._request('GET', f'/chunks/{memory_id}') if False else (
        # 不复用 endpoint, 自己合并 6 维度 (跟 deep_context_for 类似)
        {
            'chunk': self._request('GET', f'/chunks/{memory_id}'),
            'provenance': self.get_provenance(memory_id),
            'history': self.get_history(memory_id),
            'relations': self.get_relations(memory_id),
            'hierarchy': self._request('GET', f'/hierarchy/enrich/{memory_id}'),
            'links': self._request('GET', f'/links/{memory_id}').get('links', []),
        }
    )

def agent_health(self) -> Dict:
    """All-in-one health check for agent startup."""
    try:
        return {
            'server': self._request('GET', '/health'),
            'stats': self._request('GET', '/stats'),
            'pr_cache': self.pr_cache_stats(),
            'storage': self.storage_health(),
            'forgetting': self.forgetting_stats(),
        }
    except Exception as e:
        return {'error': str(e)}

# 在 MemoryCortexClient class 内追加 — patch class def
