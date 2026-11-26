"""
OpenCode cortex loader — 把 connector 暴露为 opencode 可用的 tools.

提供:
  - get_cortex_tools():  action schema 列表 (opencode agent 可 dispatch)
  - load_skill_metadata():  解析 SKILL.md frontmatter
  - build_invoker():  返回 action name → callable 的映射 (用于 runtime invoke)

与 memory_tool.MemoryTool 配合:
    invoker = build_invoker()
    result = invoker['remember'](content='hi')

设计: loader 不持有 client 实例 (per-call 注入), 让 demo 和 test 都能
monkeypatch 注入 mock client.
"""

import os
from typing import Any, Callable, Dict, List

# Dual-mode import for memory_tool
try:
    from .memory_tool import MemoryTool
except ImportError:  # pragma: no cover
    import importlib.util as _ilu
    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = _ilu.spec_from_file_location(
        'memory_tool',
        os.path.join(_here, 'memory_tool.py'),
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    MemoryTool = _mod.MemoryTool


# Action schema definitions — 每个 action 是 dict {name, description, params}
# params 是 {param_name: {type, required, default, description}}
ACTIONS: List[Dict[str, Any]] = [
    {
        "name": "remember",
        "description": "Store a memory chunk. CoALA decides ADD/UPDATE/MERGE automatically.",
        "params": {
            "content":      {"type": "string",  "required": True,  "description": "memory text"},
            "type":         {"type": "string",  "required": False, "default": "knowledge",
                              "description": "knowledge|experience|decision|context|preference"},
            "tags":         {"type": "list",    "required": False, "description": "list of tags (str or comma-separated)"},
            "importance":   {"type": "int",     "required": False, "default": 5,
                              "description": "1-10; higher = more important"},
            "session_id":   {"type": "string",  "required": False, "description": "optional, log to working memory"},
            "user_id":      {"type": "string",  "required": False, "description": "optional, isolate per user"},
            "metadata":     {"type": "dict",    "required": False, "description": "extra metadata"},
        },
    },
    {
        "name": "recall",
        "description": "Retrieve chunks relevant to query (hybrid search + optional context).",
        "params": {
            "query":          {"type": "string", "required": True,  "description": "search text"},
            "top_k":          {"type": "int",    "required": False, "default": 5,
                                "description": "1-50"},
            "include_context": {"type": "bool",  "required": False, "default": True,
                                "description": "inject working memory into result"},
            "session_id":     {"type": "string", "required": False, "description": "session for context"},
            "user_id":        {"type": "string", "required": False, "description": "filter by user"},
        },
    },
    {
        "name": "forget",
        "description": "Delete or archive memories (by chunk_id / tag / TTL / user_id).",
        "params": {
            "chunk_id":        {"type": "string", "required": False, "description": "specific chunk"},
            "tag":             {"type": "string", "required": False, "description": "delete all with this tag"},
            "expire_old_days": {"type": "int",    "required": False, "description": "TTL archive"},
            "user_id":         {"type": "string", "required": False, "description": "delete all for user"},
        },
    },
    {
        "name": "search",
        "description": "Raw POST /search; returns full backend response (total/results/rankings).",
        "params": {
            "query":      {"type": "string", "required": True,  "description": "search text"},
            "limit":      {"type": "int",    "required": False, "default": 10},
            "strategies": {"type": "string", "required": False, "default": "vector+fts5"},
            "filters":    {"type": "dict",   "required": False, "description": "extra filters"},
        },
    },
    {
        "name": "audit_query",
        "description": "Read the cortex audit log (G7).",
        "params": {
            "action":        {"type": "string", "required": False, "description": "filter by action"},
            "resource_type": {"type": "string", "required": False, "description": "filter by resource type"},
            "limit":         {"type": "int",    "required": False, "default": 50},
        },
    },
    {
        "name": "introspect",
        "description": "Self-aware metadata: health, stats, ops counters.",
        "params": {},
    },
]


def get_cortex_tools() -> List[Dict[str, Any]]:
    """Return action schema list (for opencode tool dispatch tables)."""
    return list(ACTIONS)


def load_skill_metadata(path: str = None) -> Dict[str, str]:
    """Parse SKILL.md YAML frontmatter. Returns {name, description, version}."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SKILL.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        raise ValueError(f"SKILL.md missing YAML frontmatter: {path}")
    _, fm, _ = content.split("---", 2)
    meta = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def build_invoker(tool: MemoryTool = None) -> Dict[str, Callable]:
    """Build a name → callable map. Each callable forwards to MemoryTool methods.

    Usage:
        invoker = build_invoker(MemoryTool(base_url='http://cortex:1880'))
        result = invoker['remember'](content='hi', tags='a,b')
    """
    if tool is None:
        tool = MemoryTool()
    return {
        "remember":    _make_invoker(tool, "remember"),
        "recall":      _make_invoker(tool, "recall"),
        "forget":      _make_invoker(tool, "forget"),
        "search":      _make_invoker(tool, "search"),
        "audit_query": _make_invoker(tool, "audit_query"),
        "introspect":  _make_invoker(tool, "introspect"),
    }


class _Bound:
    """Wraps a bound method with `.tool` attr for monkeypatching in tests.

    __call__ delegates to self.tool(), default = self._method.
    Tests can replace `invoker['remember'].tool = lambda **kw: fake` to stub.
    """

    def __init__(self, tool: MemoryTool, method_name: str):
        self._tool = tool
        self._method = getattr(tool, method_name)
        self.tool = self._method

    def __call__(self, *args, **kwargs):
        return self.tool(*args, **kwargs)


def _make_invoker(tool: MemoryTool, method_name: str) -> _Bound:
    return _Bound(tool, method_name)