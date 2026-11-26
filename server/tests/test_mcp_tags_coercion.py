"""Regression tests for V6.5+ tags-coercion fix.

FastMCP 3.x degrades bare ``list`` type hints to ``{"type":"string"}`` in the
MCP tool schema, which means clients sometimes hand us a comma-separated
string while others hand us a real list. Both shapes must end up as a
clean ``list[str]`` before hitting the Pydantic ``MemoryChunkCreate.tags``
validator (``List[str]``).

These tests do NOT hit the live Cortex server; they only exercise the
``_coerce_tags`` helper so the regex/string-handling is locked down.
"""
from __future__ import annotations

import sys
import types
import unittest


# Stub out ``fastmcp`` so importing the live module doesn't require the
# docker-only dependency. The real ``_coerce_tags`` function is pure-Python
# (no fastmcp symbols used inside), so a stub module is enough.
class _FakeToolManager:
    def __init__(self): self._tools = []
    def list_tools(self): return []


class _FakeApp:
    router = types.SimpleNamespace(lifespan_context=lambda *_a, **_kw: _AsyncCM())


class _AsyncCM:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _FakeFastMCP:
    def __init__(self, *a, **kw):
        self._tool_manager = _FakeToolManager()
    def tool(self, *a, **kw):
        def deco(fn): return fn
        return deco
    def resource(self, *a, **kw):
        def deco(fn): return fn
        return deco
    def http_app(self, *a, **kw):
        return _FakeApp()


_fastmcp_stub = types.ModuleType("fastmcp")
_fastmcp_stub.FastMCP = _FakeFastMCP
sys.modules.setdefault("fastmcp", _fastmcp_stub)

sys.path.insert(0, r".server\src")

from mcp_server.cortex_mcp_server import _coerce_tags  # noqa: E402


class TestCoerceTags(unittest.TestCase):
    def test_none_default_none(self):
        self.assertEqual(_coerce_tags(None), [])

    def test_none_default_list(self):
        self.assertEqual(_coerce_tags(None, default=["a", "b"]), ["a", "b"])

    def test_string_comma(self):
        self.assertEqual(_coerce_tags("a,b,c"), ["a", "b", "c"])

    def test_string_whitespace_and_empty(self):
        self.assertEqual(_coerce_tags(" a , , b ,  "), ["a", "b"])

    def test_string_newline(self):
        self.assertEqual(_coerce_tags("a\nb,c"), ["a", "b", "c"])

    def test_list_passthrough(self):
        self.assertEqual(_coerce_tags(["a", "b", "c"]), ["a", "b", "c"])

    def test_list_trim(self):
        self.assertEqual(_coerce_tags(["  a  ", "b", "", None]), ["a", "b"])

    def test_tuple(self):
        self.assertEqual(_coerce_tags(("x", "y")), ["x", "y"])

    def test_int_other(self):
        # Last-ditch fallback: non-list/str gets stringified.
        self.assertEqual(_coerce_tags(42), ["42"])

    def test_empty_string(self):
        self.assertEqual(_coerce_tags(""), [])

    def test_legacy_passthrough(self):
        # Connector uses list[str] directly; helper must not mutate.
        result = _coerce_tags(["hello", "world"])
        self.assertEqual(result, ["hello", "world"])
        self.assertIs(type(result), list)


class TestConnectorCortexClientCompat(unittest.TestCase):
    """Confirm ``cortex_client.remember`` accepts both list and comma-string
    for the ``tags`` argument, so that direct SDK callers (Arcadia connector,
    one-off scripts, etc.) also get the dual-path behavior."""

    def test_list_passthrough(self):
        from sdk.cortex_client import MemoryCortexClient
        client = MemoryCortexClient("http://127.0.0.1:1", timeout=1)
        captured = {}

        def fake_request(method, path, params=None, body=None, **_):
            captured["body"] = body
            return {"id": "fake_id", "content": body["content"], "type": body["type"],
                    "tags": body.get("tags", []), "importance": body.get("importance", 5),
                    "metadata": body.get("metadata", {})}

        client._request = fake_request  # type: ignore
        client.remember(content="hello", tags=["a", "b"])
        self.assertEqual(captured["body"]["tags"], ["a", "b"])
        self.assertEqual(captured["body"]["content"], "hello")

    def test_comma_string(self):
        from sdk.cortex_client import MemoryCortexClient
        client = MemoryCortexClient("http://127.0.0.1:1", timeout=1)
        captured = {}

        def fake_request(method, path, params=None, body=None, **_):
            captured["body"] = body
            return {"id": "fake_id", "content": body["content"], "type": body["type"],
                    "tags": body.get("tags", []), "importance": body.get("importance", 5),
                    "metadata": body.get("metadata", {})}

        client._request = fake_request  # type: ignore
        client.remember(content="hello", tags="a,b,c")
        # Comma-separated string must be coerced to a real list before send.
        self.assertEqual(captured["body"]["tags"], ["a", "b", "c"])
        self.assertEqual(captured["body"]["content"], "hello")

    def test_none_tags_no_payload(self):
        from sdk.cortex_client import MemoryCortexClient
        client = MemoryCortexClient("http://127.0.0.1:1", timeout=1)
        captured = {}

        def fake_request(method, path, params=None, body=None, **_):
            captured["body"] = body
            return {"id": "fake_id", "content": body["content"], "type": body["type"]}

        client._request = fake_request  # type: ignore
        client.remember(content="hello")  # no tags
        self.assertNotIn("tags", captured["body"])


if __name__ == "__main__":
    unittest.main()
