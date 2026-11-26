"""
Memory Tool facade for opencode agents.

在 cortex_client 之上提供 6 个 agentic actions + 1 个 batch:
  - remember      写入记忆 (CoALA 自动 ADD/UPDATE/MERGE)
  - recall        召回到相关记忆 (hybrid + user_id 客户端过滤)
  - forget        删除 / TTL 归档
  - search        raw POST /search wrapper (返回 raw 后端结果)
  - audit_query   读 audit log
  - introspect    自检 (health / stats / ops counters)
  - batch         串行批量执行 operations

输入校验 + 统一 CortexError 异常 + 错误传播.

Usage:
    from connector.opencode.memory_tool import MemoryTool

    tool = MemoryTool(base_url='http://127.0.0.1:1880')
    chunk = tool.remember('I prefer Python', tags='python,backend')
    hits = tool.recall('Python preference', top_k=5)
    tool.forget(chunk_id=chunk['id'])
"""

from typing import List, Dict, Optional, Any
import os as _os
import importlib.util as _ilu

# Re-export CortexError + MemoryCortexClient.
# Dual-mode import:
#   - package-style: `from connector.opencode.memory_tool import ...`
#   - standalone:    tests load via importlib.util.spec_from_file_location
# In standalone mode, fall back to loading cortex_client.py by path.
try:
    from .cortex_client import CortexError, MemoryCortexClient  # type: ignore
except ImportError:  # pragma: no cover
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _spec = _ilu.spec_from_file_location(
        'cortex_client',
        _os.path.join(_here, 'cortex_client.py'),
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    CortexError = _mod.CortexError
    MemoryCortexClient = _mod.MemoryCortexClient


class MemoryTool:
    """opencode-friendly facade over MemoryCortexClient.

    关键设计:
      - 输入校验 (空 content, importance 1-10, top_k 1-50) → CortexError 400
      - 错误传播: client 返回 {'error': ..., 'status': ...} → CortexError
      - 类型注解 + dataclass-style 文档 (no runtime cost)
    """

    MAX_TOP_K = 50
    MAX_IMPORTANCE = 10
    MIN_IMPORTANCE = 1

    def __init__(self, base_url: str = None, client: MemoryCortexClient = None):
        self.client = client or MemoryCortexClient(base_url=base_url)

    # ---------- write path ----------

    def remember(self, content: str,
                 type: str = "knowledge",
                 tags=None,
                 importance: int = 5,
                 session_id: str = None,
                 user_id: str = None,
                 metadata: Dict = None) -> Dict:
        """写入一段记忆. CoALA 自动决策."""
        if not content or not content.strip():
            raise CortexError("content must be non-empty", status_code=400)
        if not (self.MIN_IMPORTANCE <= importance <= self.MAX_IMPORTANCE):
            raise CortexError(
                f"importance must be in [{self.MIN_IMPORTANCE}, {self.MAX_IMPORTANCE}], got {importance}",
                status_code=400,
            )
        result = self.client.remember(
            content=content,
            type=type,
            tags=tags,
            importance=importance,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
        )
        self._raise_if_error(result)
        return result

    # ---------- read path ----------

    def recall(self, query: str,
               top_k: int = 5,
               include_context: bool = True,
               session_id: str = None,
               user_id: str = None):
        """召回相关记忆."""
        if not query or not query.strip():
            raise CortexError("query must be non-empty", status_code=400)
        if not (1 <= top_k <= self.MAX_TOP_K):
            raise CortexError(
                f"top_k must be in [1, {self.MAX_TOP_K}], got {top_k}",
                status_code=400,
            )
        result = self.client.recall(
            query=query,
            top_k=top_k,
            include_context=include_context,
            session_id=session_id,
            user_id=user_id,
        )
        # recall 没有标准 error shape (后端 list 不带 error dict), 由 client 处理
        return result

    # ---------- delete path ----------

    def forget(self, chunk_id: str = None, tag: str = None,
               expire_old_days: int = None,
               user_id: str = None) -> Dict:
        """删除 / TTL 归档记忆. 至少需要一个参数."""
        if not any([chunk_id, tag, expire_old_days is not None, user_id]):
            raise CortexError(
                "must specify chunk_id, tag, user_id, or expire_old_days",
                status_code=400,
            )
        result = self.client.forget(
            chunk_id=chunk_id,
            tag=tag,
            expire_old_days=expire_old_days,
            user_id=user_id,
        )
        self._raise_if_error(result)
        return result

    # ---------- raw search (不解析) ----------

    def search(self, query: str,
               limit: int = 10,
               strategies: str = "vector+fts5",
               filters: Dict = None) -> Dict:
        """raw POST /search. 返回后端原始响应 (含 total/results/rankings)."""
        if not query:
            raise CortexError("query must be non-empty", status_code=400)
        body: Dict = {"query": query, "limit": limit, "strategies": strategies}
        if filters:
            body["filters"] = filters
        result = self.client._request("POST", "/search", body=body)
        self._raise_if_error(result)
        return result

    # ---------- audit log reader ----------

    def audit_query(self, action: str = None,
                    resource_type: str = None,
                    limit: int = 50) -> Dict:
        """读 audit log (G7). filter by action / resource_type."""
        if limit < 1 or limit > 1000:
            raise CortexError("limit must be in [1, 1000]", status_code=400)
        body: Dict = {"limit": limit}
        if action:
            body["action"] = action
        if resource_type:
            body["resource_type"] = resource_type
        result = self.client._request("POST", "/audit/query", body=body)
        self._raise_if_error(result)
        return result

    # ---------- introspect ----------

    def introspect(self) -> Dict:
        """自检 metadata (health + stats + ops counters)."""
        result = self.client.introspect()
        # introspect 嵌套 dict, 不直接 raise (health 可能返回 503 但其他 OK)
        return result

    # ---------- batch operations ----------

    def batch(self, operations: List[Dict[str, Any]]) -> List[Dict]:
        """串行执行多个 actions. 每个 operation 是一个 dict,
        必须包含 'action' key (remember / forget / search),
        其余 key 作为参数转发.

        Example:
            batch([
              {'action': 'remember', 'content': 'x', 'tags': 'a,b'},
              {'action': 'forget', 'chunk_id': 'y'},
            ])
        """
        results = []
        for op in operations:
            action = op.get("action")
            if action == "remember":
                r = self.remember(**{k: v for k, v in op.items() if k != "action"})
            elif action == "forget":
                r = self.forget(**{k: v for k, v in op.items() if k != "action"})
            elif action == "search":
                r = self.search(**{k: v for k, v in op.items() if k != "action"})
            else:
                raise CortexError(f"unknown batch action: {action!r}", status_code=400)
            results.append(r)
        return results

    # ---------- error propagation helper ----------

    def _raise_if_error(self, result: Any) -> None:
        """如果 result 是 {'error': ..., 'status': ...}, 抛 CortexError."""
        if isinstance(result, dict) and "error" in result:
            raise CortexError(
                result["error"],
                status_code=result.get("status"),
                body=result.get("body"),
            )