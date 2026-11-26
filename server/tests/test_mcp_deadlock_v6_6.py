"""Regression tests for the V6.6.0 MCP self-deadlock fix.

Symptom (observed live on 127.0.0.1, 2026-10-30)
-------------------------------------------------
Every MCP tool call took exactly 30.0s and returned::

    {"error": "timed out"}      with isError=False

The scheduled-maintenance scripts (``cortex_maint_daily.py`` /
``cortex_maint_weekly.py``) drive the whole maintenance path over MCP, so
every daily run logged ``OK <tool> -> {"error": "timed out"}`` and
``errors=0``. The cron job looked healthy while doing nothing at all.

Root cause
----------
Every tool handler in ``cortex_mcp_server.py`` reaches Cortex through
``_CORTEX_CLIENT`` (``http://127.0.0.1:1880``) — the very server that is
executing the MCP call. FastMCP runs *sync* handlers inline on the event
loop, so the handler's blocking ``urllib.request.urlopen`` loopback
deadlocks: the loop is blocked waiting for a response that only the blocked
loop could serve. The SDK's 30s timeout then fires.

Fix
---
``_offload_sync_handler()`` wraps sync handlers so the blocking call runs in
a worker thread, leaving the event loop free to serve the loopback request.

Invariants pinned here:
1. ``_offload_sync_handler`` exists and is applied to every registered tool.
2. Async handlers pass through untouched.
3. Sync handlers become coroutine functions.
4. ``functools.wraps`` is used so FastMCP still derives the same schema.
5. The offload really uses a worker thread (``anyio.to_thread.run_sync``).
6. No handler is registered unwrapped in ``_build_mcp``.

These tests do NOT need Postgres or a live server — they exercise the
wrapper directly and grep the source for the wiring.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import types
import unittest


SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
MCP_SERVER = os.path.join(SRC_DIR, "mcp_server", "cortex_mcp_server.py")

with open(MCP_SERVER, encoding="utf-8") as f:
    SOURCE = f.read()


def _install_fastmcp_stub() -> None:
    """Stub ``fastmcp`` so the module imports without the docker-only dep.

    ``_offload_sync_handler`` only uses ``functools``/``inspect``/``anyio``,
    so a stub is enough to import and unit-test it.
    """
    if "fastmcp" in sys.modules:
        return
    try:
        import fastmcp  # noqa: F401
        return
    except ImportError:
        pass

    stub = types.ModuleType("fastmcp")

    class _FakeToolManager:
        def __init__(self):
            self._tools = {}

        def list_tools(self):
            return list(self._tools.values())

        def get_tool(self, name):
            return self._tools.get(name)

    class _FakeFastMCP:
        def __init__(self, *a, **kw):
            self._tool_manager = _FakeToolManager()
            self.resources = {}

        def tool(self, name=None, description=None):
            def deco(fn):
                self._tool_manager._tools[name or fn.__name__] = fn
                return fn
            return deco

        def resource(self, uri):
            def deco(fn):
                self.resources[uri] = fn
                return fn
            return deco

        def http_app(self, *a, **kw):
            return types.SimpleNamespace(router=types.SimpleNamespace(
                lifespan_context=lambda *_a, **_kw: None))

    stub.FastMCP = _FakeFastMCP

    mcp_pkg = types.ModuleType("mcp")
    mcp_server_pkg = types.ModuleType("mcp.server")
    mcp_stream = types.ModuleType("mcp.server.streamable_http_manager")

    class _FakeSessionManager:
        def run(self):
            raise RuntimeError("not used in unit tests")

    mcp_stream.StreamableHTTPSessionManager = _FakeSessionManager
    sys.modules.setdefault("mcp", mcp_pkg)
    sys.modules.setdefault("mcp.server", mcp_server_pkg)
    sys.modules.setdefault("mcp.server.streamable_http_manager", mcp_stream)
    sys.modules["fastmcp"] = stub


_install_fastmcp_stub()
for _p in (SRC_DIR, os.path.join(SRC_DIR, "sdk"), os.path.join(SRC_DIR, "mcp_server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cortex_mcp_server_under_test", MCP_SERVER
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


def _list_tools(mcp):
    """Return ``(name, callable)`` pairs for every registered tool.

    fastmcp 2.x exposes an *async* ``ToolManager.get_tools()`` returning
    ``Tool`` objects (the handler lives on ``Tool.fn``); the stub used when
    the real dependency is absent exposes a sync ``list_tools()`` returning
    the raw callables. Support both so the test runs with and without the
    docker-only dependency installed.
    """
    tm = mcp._tool_manager

    def _normalize(items):
        pairs = []
        if isinstance(items, dict):
            items = items.items()
        for item in items:
            if isinstance(item, tuple):
                name, obj = item
            else:
                name, obj = getattr(item, "name", "?"), item
            fn = getattr(obj, "fn", obj)
            pairs.append((name, fn))
        return pairs

    fn = getattr(tm, "get_tools", None)
    if callable(fn):
        result = fn()
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(result)
            else:  # pragma: no cover - only hit inside a live loop
                raise AssertionError(
                    "_list_tools() must not be called from a running event loop"
                )
        return _normalize(result)

    fn = getattr(tm, "list_tools", None)
    if callable(fn):
        return _normalize(fn())
    return _normalize(getattr(tm, "_tools", {}))


class TestOffloadSyncHandler(unittest.TestCase):
    """Unit tests for the wrapper itself."""

    def test_sync_handler_becomes_coroutine_function(self):
        def handler(limit: int = 5) -> dict:
            return {"limit": limit}

        wrapped = MOD._offload_sync_handler(handler)
        self.assertTrue(
            inspect.iscoroutinefunction(wrapped),
            "sync handler must be wrapped into a coroutine function",
        )

    def test_async_handler_passed_through_untouched(self):
        async def handler() -> dict:
            return {}

        wrapped = MOD._offload_sync_handler(handler)
        self.assertIs(
            wrapped, handler,
            "async handlers already yield the loop and must not be rewrapped",
        )

    def test_wrapper_preserves_name_and_doc(self):
        def my_handler(limit: int = 5) -> dict:
            """Doc string here."""
            return {}

        wrapped = MOD._offload_sync_handler(my_handler)
        self.assertEqual(wrapped.__name__, "my_handler")
        self.assertEqual(wrapped.__doc__, "Doc string here.")
        self.assertTrue(
            hasattr(wrapped, "__wrapped__"),
            "functools.wraps must set __wrapped__ so FastMCP derives the "
            "same input schema",
        )

    def test_wrapper_preserves_call_signature(self):
        def handler(limit: int = 5, tag: str = "x") -> dict:
            return {}

        wrapped = MOD._offload_sync_handler(handler)
        params = inspect.signature(wrapped).parameters
        self.assertEqual(list(params), ["limit", "tag"])
        self.assertEqual(params["limit"].default, 5)
        self.assertEqual(params["tag"].default, "x")

    def test_offloaded_call_returns_handler_result(self):
        def handler(value: int) -> dict:
            return {"value": value * 2}

        wrapped = MOD._offload_sync_handler(handler)
        result = asyncio.run(wrapped(value=21))
        self.assertEqual(result, {"value": 42})

    def test_offloaded_call_runs_off_the_event_loop(self):
        """The blocking work must not run on the event loop thread."""
        import threading

        loop_thread = threading.current_thread().ident
        seen = {}

        def handler() -> dict:
            seen["thread"] = threading.current_thread().ident
            return {}

        wrapped = MOD._offload_sync_handler(handler)
        asyncio.run(wrapped())
        self.assertNotEqual(
            seen.get("thread"), loop_thread,
            "handler must run in a worker thread, not on the event loop",
        )

    def test_handler_exception_propagates(self):
        def handler() -> dict:
            raise RuntimeError("boom")

        wrapped = MOD._offload_sync_handler(handler)
        with self.assertRaises(RuntimeError):
            asyncio.run(wrapped())


class TestBuildMcpWiring(unittest.TestCase):
    """The wiring inside _build_mcp must apply the wrapper to every tool."""

    def test_every_tool_is_registered_through_the_wrapper(self):
        body = SOURCE[SOURCE.index("def _build_mcp()"):]
        loop = body[: body.index("@mcp.resource")]
        self.assertIn(
            "_offload_sync_handler(handler)", loop,
            "tools must be registered through _offload_sync_handler()",
        )
        self.assertNotIn(
            "description=description)(handler)", loop,
            "a raw unwrapped handler must not be registered",
        )

    def test_tool_count_is_unchanged(self):
        self.assertIn(
            "assert len(_EXTENDED_TOOLS) == 98", SOURCE,
            "tool catalog size assertion must stay in place",
        )
        self.assertEqual(len(MOD.TOOLS), 104)

    def test_all_registered_handlers_are_now_async(self):
        """After wrapping, every registered tool handler must be awaitable."""
        mcp = MOD._build_mcp()
        tools = _list_tools(mcp)
        self.assertTrue(tools, "expected a non-empty tool catalog")
        sync_leftovers = [
            name for name, fn in tools if not inspect.iscoroutinefunction(fn)
        ]
        self.assertEqual(
            sync_leftovers, [],
            "every tool handler must be offloaded to a worker thread",
        )


class TestOffloadUsesWorkerThread(unittest.TestCase):
    """The wrapper must actually use anyio's thread pool."""

    def test_source_uses_anyio_to_thread(self):
        m = re.search(
            r"def _offload_sync_handler.*?\n\n\n", SOURCE, re.S
        )
        self.assertIsNotNone(m, "_offload_sync_handler not found in source")
        body = m.group(0)
        self.assertIn("anyio.to_thread.run_sync", body)
        self.assertIn("functools.wraps", body)


if __name__ == "__main__":
    unittest.main()
