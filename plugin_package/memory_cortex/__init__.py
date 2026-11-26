#!/usr/bin/env python3
"""
Memory Cortex - Hermes Memory Provider Plugin

注册为Hermes的memory provider插件，实现三个钩子：
1. pre_llm_call: 自动注入语义记忆（对应00_memory_inject.py）
2. post_tool_call: 自动持久化工具调用经验（对应00_tool_memory_persist.py）  
3. on_session_end: 自动持久化session memory（对应01_memory_session_persist.py）

修复版本：prefetch() 方法现在会调用 Memory Cortex 搜索 API 并返回相关记忆
"""

import json
import os
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List
import time

logger = logging.getLogger(__name__)

# The connector/backend is the single HTTP integration path. The default is the
# remote lab service used by Hermes on the Windows host; deployments should set
# MEMORY_CORTEX_URL explicitly rather than relying on this fallback.
CORTEX_URL = os.getenv("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")


def _cortex_url(path: str) -> str:
    return f"{CORTEX_URL.rstrip('/')}/{path.lstrip('/')}"


def _request_json(method: str, path: str, *, payload=None, timeout: float = 10):
    import requests
    response = requests.request(method, _cortex_url(path), json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()

def _run_hook(hook_name: str, **kwargs) -> dict:
    """运行一个钩子脚本，返回结果。"""
    hook_script = HOOKS_DIR / f"{hook_name}.py"
    if not hook_script.exists():
        return {"status": "skipped", "reason": f"hook script not found: {hook_script}"}
    
    try:
        import subprocess
        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(Path(__file__).parent.parent.parent.parent.parent / "scripts"))
        result = subprocess.run(
            [sys.executable, str(hook_script)],
            input=json.dumps(kwargs),
            capture_output=True,
            text=True,
            timeout=30,
            env=env
        )
        if result.returncode != 0:
            logger.warning("Hook %s failed: %s", hook_name, result.stderr[:200])
            return {"status": "failed", "error": result.stderr[:500]}
        return json.loads(result.stdout.strip()) if result.stdout.strip() else {"status": "ok"}
    except Exception as e:
        logger.warning("Hook %s error: %s", hook_name, e)
        return {"status": "failed", "error": str(e)}

def _search_memory_cortex(query: str, limit: int = 3, min_similarity: float = 0.4) -> List[Dict[str, Any]]:
    """调用 V5.3 hybrid search API，使用服务端返回的 similarity/score。"""
    try:
        data = _request_json(
            "POST", "/search",
            payload={"query": query, "limit": limit, "include_related": True},
            timeout=10,
        )
        results = data.get("results", []) if isinstance(data, dict) else []
        filtered = []
        for result in results:
            similarity = result.get("similarity")
            if similarity is None:
                similarity = result.get("composite_score", result.get("rrf_score", 0.0))
            try:
                similarity = float(similarity)
            except (TypeError, ValueError):
                similarity = 0.0
            if similarity >= min_similarity:
                result["similarity"] = similarity
                filtered.append(result)
        return filtered
    except Exception as e:
        logger.debug("Memory Cortex search error: %s", e)
        return []

def register(collector):
    """注册为Hermes memory provider插件。"""
    # 延迟导入，只在 Hermes 环境中执行
    # 注意：MemoryProvider 在 agent.memory_provider 中，不在 plugins.memory 中
    try:
        from agent.memory_provider import MemoryProvider
    except ImportError:
        # 回退到 plugins.memory（旧版本兼容）
        from plugins.memory import MemoryProvider
    
    class MemoryCortexProvider(MemoryProvider):
        @property
        def name(self) -> str:
            return "memory-cortex"
        
        def is_available(self) -> bool:
            # 检查服务是否可用
            try:
                resp = _request_json("GET", "/health", timeout=3)
                return resp.get("status") == "healthy"
            except:
                return False
        
        def initialize(self, session_id: str, **kwargs) -> None:
            pass
        
        def system_prompt_block(self) -> str:
            return ""
        
        def prefetch(self, query: str, *, session_id: str = "") -> str:
            """搜索相关记忆并返回格式化文本，用于对话前注入"""
            if not query or not query.strip():
                return ""
            
            try:
                # 搜索相关记忆
                results = _search_memory_cortex(query, limit=3, min_similarity=0.4)
                if not results:
                    return ""
                
                # 格式化记忆文本
                formatted = "【相关历史记忆】\n"
                for i, mem in enumerate(results, 1):
                    content = mem.get("content", "").strip()
                    if not content:
                        continue
                    
                    # 获取标签
                    tags = mem.get("tags", [])
                    tag_str = f" ({', '.join(tags)})" if tags else ""
                    
                    # 获取相似度
                    similarity = mem.get("similarity", 0.0)
                    sim_pct = int(similarity * 100)
                    
                    # 格式化条目
                    formatted += f"{i}. [{sim_pct}%] {content[:200]}{'...' if len(content) > 200 else ''}{tag_str}\n"
                
                return formatted.strip()
            except Exception as e:
                logger.debug("Memory Cortex prefetch error: %s", e)
                return ""
        
        def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
            """持久化对话轮次。"""
            try:
                _run_hook("on_session_end",
                    user_content=user_content[:500],
                    assistant_content=assistant_content[:500],
                    session_id=session_id
                )
            except Exception as e:
                logger.debug("sync_turn persist error: %s", e)
        
        def get_tool_schemas(self) -> List[Dict[str, Any]]:
            return [
                {
                    "name": "cortex_search",
                    "description": "Hybrid semantic, lexical and graph-aware memory search.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "cortex_health",
                    "description": "Return Memory Cortex service health.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]

        def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
            if tool_name == "cortex_health":
                result = _request_json("GET", "/health", timeout=5)
            elif tool_name == "cortex_search":
                query = str(args.get("query", "")).strip()
                if not query:
                    return json.dumps({"error": "query is required"}, ensure_ascii=False)
                result = _request_json(
                    "POST", "/search",
                    payload={
                        "query": query,
                        "limit": min(max(int(args.get("limit", 5)), 1), 20),
                        "include_related": True,
                    },
                    timeout=15,
                )
            else:
                result = {"error": f"unsupported tool: {tool_name}"}
            return json.dumps(result, ensure_ascii=False, default=str)
        
        def shutdown(self) -> None:
            pass
        
        def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
            """会话结束时的自动持久化。"""
            try:
                # 提取session memory内容
                session_text = ""
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        session_text += f"[{role}] {content[:200]}\n"
                
                _run_hook("on_session_end",
                    session_memory=session_text[:5000],
                    session_id="",
                    turn_count=len(messages)
                )
            except Exception as e:
                logger.debug("on_session_end error: %s", e)
        
        def on_memory_write(self, action: str, target: str, content: str) -> None:
            """镜像Hermes内置记忆写入到Memory Cortex。"""
            try:
                # The built-in Cortex takeover/backend is the single write owner.
                # This provider must not mirror the same write into /chunks.
                logger.debug("Skipping provider on_memory_write; takeover owns persistence")
            except Exception as e:
                logger.debug("on_memory_write guard error: %s", e)
    
    collector.register_memory_provider(MemoryCortexProvider())
