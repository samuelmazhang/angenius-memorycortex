"""Unit tests for Cortex v6.5.0 consolidation pipeline (#A.1).

Covers 5 required tests + 3 helper / regression tests:

  1. test_extract_returns_valid_schema           -- schema sanity
  2. test_extract_handles_llm_unreachable        -- network failure -> empty dict
  3. test_extract_retries_on_failure             -- tenacity retry behavior
  4. test_extract_respects_timeout               -- timeout=cfg.llm_timeout
  5. test_extract_facts_have_confidence          -- every fact has confidence in [0,1]
  6. test_extract_short_circuits_empty_content   -- no LLM call for empty chunk
  7. test_normalize_drops_invalid_facts          -- bad confidence / missing field
  8. test_extract_strips_json_fence              -- LLM ```json ``` wrapping

Tests use unittest.TestCase + pytest-asyncio (asyncio_mode=STRICT).
httpx.AsyncClient is monkey-patched via mock so no live HTTP / LLM endpoint
is required.
"""

import os
import sys
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from consolidate import extract as extract_mod  # noqa: E402
from consolidate.extract import (  # noqa: E402
    EMPTY_RESULT,
    EXTRACT_SYSTEM,
    extract,
    _normalize_fact,
    _normalize_timeline,
    _normalize_preference,
    _normalize,
    _strip_json_fence,
)


def _make_cfg(
    url: str = "http://fake-llm/v1/chat/completions",
    api_key: str = "sk-test",
    model: str = "gpt-3.5-turbo",
    timeout: float = 15.0,
) -> SimpleNamespace:
    """Build a duck-typed cfg (CortexConfig requires .env -- can't import in tests)."""
    return SimpleNamespace(
        llm_url=url,
        llm_api_key=api_key,
        llm_model=model,
        llm_timeout=timeout,
    )


def _make_mock_response(payload: dict) -> MagicMock:
    """Build a mock httpx Response returning ``payload`` from .json()."""
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


def _make_mock_client(response: MagicMock | None = None, side_effect: Exception | None = None) -> AsyncMock:
    """Build a mock httpx.AsyncClient.

    Supports:
      - response: a MagicMock to return on .post() (success path)
      - side_effect: an exception to raise on .post() (failure path)

    AsyncClient is an async context manager; we mock __aenter__/__aexit__.
    """
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    if side_effect is not None:
        client.post = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=response)
    return client


# ====== 1. Schema validity ======

@pytest.mark.asyncio
async def test_extract_returns_valid_schema():
    """Mock a successful LLM response; extract() must return the 3-key envelope."""
    llm_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "facts": [
                            {"entity": "Alice", "predicate": "age", "value": 30, "confidence": 0.9},
                            {"entity": "Alice", "predicate": "city", "value": "Beijing", "confidence": 0.7},
                        ],
                        "timeline": [
                            {"date": "2026-01-15", "event": "joined Hermes"},
                        ],
                        "preferences": [
                            {"key": "language", "value": "Python", "polarity": "like"},
                        ],
                    })
                }
            }
        ]
    }
    resp = _make_mock_response(llm_payload)
    mock_client = _make_mock_client(response=resp)

    cfg = _make_cfg()
    chunk = {"id": "c1", "content": "Alice is 30 and lives in Beijing, prefers Python."}

    with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
        result = await extract(chunk, [], cfg)

    assert set(result.keys()) == {"facts", "timeline", "preferences"}
    assert isinstance(result["facts"], list)
    assert isinstance(result["timeline"], list)
    assert isinstance(result["preferences"], list)
    assert len(result["facts"]) == 2
    assert len(result["timeline"]) == 1
    assert len(result["preferences"]) == 1


# ====== 2. LLM unreachable -> empty dict, no raise ======

@pytest.mark.asyncio
async def test_extract_handles_llm_unreachable():
    """httpx transport error must not propagate; extract() returns empty envelope."""
    # Use httpx.ConnectError -- it subclasses both httpx.HTTPError and
    # builtin ConnectionError, both of which are in the retry policy.
    import httpx as _httpx
    mock_client = _make_mock_client(side_effect=_httpx.ConnectError("ECONNREFUSED"))
    cfg = _make_cfg(url="http://down-host/v1/chat/completions")
    chunk = {"id": "c1", "content": "Some user statement."}

    with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
        result = await extract(chunk, [], cfg)

    assert result == {"facts": [], "timeline": [], "preferences": []}
    # Retries happened: post was called more than once.
    assert mock_client.post.await_count >= 2
    assert mock_client.post.await_count <= 3  # tenacity stop_after_attempt(3)


# ====== 3. Retries on failure ======

@pytest.mark.asyncio
async def test_extract_retries_on_failure():
    """First call raises transport error, second succeeds. extract() must use the second result.

    Uses httpx.TimeoutException (in the retry policy) so tenacity actually retries.
    """
    import httpx as _httpx
    fail_resp = _make_mock_response({"choices": [{"message": {"content": "{}"}}]})
    # raise_for_status() raising is NOT in the retry policy (generic Exception).
    # Use timeout exception on .post() instead, which IS in the retry policy.
    timeout_exc = _httpx.TimeoutException("read timed out")

    success_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "facts": [{"entity": "X", "predicate": "y", "value": "z", "confidence": 0.5}],
                        "timeline": [],
                        "preferences": [],
                    })
                }
            }
        ]
    }
    success_resp = _make_mock_response(success_payload)

    # Sequence: [timeout, success_resp] -- tenacity retries the 2nd attempt
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=[timeout_exc, success_resp])

    cfg = _make_cfg()
    chunk = {"id": "c1", "content": "Some content."}

    with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
        result = await extract(chunk, [], cfg)

    assert mock_client.post.await_count == 2
    assert len(result["facts"]) == 1
    assert result["facts"][0]["entity"] == "X"


# ====== 4. Timeout respected ======

@pytest.mark.asyncio
async def test_extract_respects_timeout():
    """httpx.AsyncClient is constructed with timeout=cfg.llm_timeout."""
    cfg = _make_cfg(timeout=42.0)
    captured = {}

    class FakeAsyncClient:
        def __init__(self, timeout=None, **kw):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json=None, headers=None):
            return _make_mock_response({
                "choices": [{"message": {"content": "{}"}}]
            })

    chunk = {"id": "c1", "content": "Some content."}
    with patch("consolidate.extract.httpx.AsyncClient", FakeAsyncClient):
        await extract(chunk, [], cfg)

    assert captured["timeout"] == 42.0


# ====== 5. Every fact has confidence in [0,1] ======

@pytest.mark.asyncio
async def test_extract_facts_have_confidence():
    """LLM response with mixed valid/invalid facts -> only valid kept."""
    llm_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "facts": [
                            {"entity": "A", "predicate": "p", "value": "v", "confidence": 0.5},  # ok
                            {"entity": "B", "predicate": "p", "value": "v", "confidence": 1.2},  # >1 drop
                            {"entity": "C", "predicate": "p", "value": "v", "confidence": -0.1},  # <0 drop
                            {"entity": "D", "predicate": "p", "value": "v"},  # missing conf drop
                            {"entity": "E", "predicate": "p", "value": "v", "confidence": 0.0},  # ok (boundary)
                            {"entity": "F", "predicate": "p", "value": "v", "confidence": 1.0},  # ok (boundary)
                        ],
                        "timeline": [],
                        "preferences": [],
                    })
                }
            }
        ]
    }
    resp = _make_mock_response(llm_payload)
    mock_client = _make_mock_client(response=resp)

    cfg = _make_cfg()
    chunk = {"id": "c1", "content": "Mixed fact quality."}

    with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
        result = await extract(chunk, [], cfg)

    # Only 3 valid facts (0.5, 0.0, 1.0)
    assert len(result["facts"]) == 3
    for f in result["facts"]:
        assert "confidence" in f
        assert 0.0 <= f["confidence"] <= 1.0


# ====== 6. Short-circuit on empty content ======

@pytest.mark.asyncio
async def test_extract_short_circuits_empty_content():
    """Empty/whitespace chunk -> empty envelope, NO LLM call."""
    mock_client = _make_mock_client()
    cfg = _make_cfg()
    chunk = {"id": "c1", "content": ""}

    with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
        result = await extract(chunk, [], cfg)

    assert result == {"facts": [], "timeline": [], "preferences": []}
    assert mock_client.post.await_count == 0  # never called


# ====== 7. Normalizer drops invalid facts ======

def test_normalize_drops_invalid_facts():
    """_normalize_fact must reject: non-dict, missing conf, out-of-range conf."""
    # None of these should pass
    assert _normalize_fact(None) is None
    assert _normalize_fact("not a dict") is None
    assert _normalize_fact({"entity": "A", "predicate": "p", "value": "v"}) is None
    assert _normalize_fact({"entity": "A", "predicate": "p", "value": "v", "confidence": "0.5"}) is None
    assert _normalize_fact({"entity": "A", "predicate": "p", "value": "v", "confidence": 1.5}) is None
    assert _normalize_fact({"entity": "A", "predicate": "p", "value": "v", "confidence": -0.5}) is None
    assert _normalize_fact({"predicate": "p", "value": "v", "confidence": 0.5}) is None
    assert _normalize_fact({"entity": "A", "value": "v", "confidence": 0.5}) is None
    # This one passes
    good = _normalize_fact({"entity": "A", "predicate": "p", "value": "v", "confidence": 0.5})
    assert good == {"entity": "A", "predicate": "p", "value": "v", "confidence": 0.5}


def test_normalize_handles_multiple_idioms():
    """LLM may emit subject/relation/object or head/type/tail -- all must work."""
    idiom_a = _normalize_fact({"entity": "A", "predicate": "p", "value": "v", "confidence": 0.7})
    idiom_b = _normalize_fact({"subject": "A", "relation": "p", "object": "v", "confidence": 0.7})
    idiom_c = _normalize_fact({"head": "A", "type": "p", "tail": "v", "confidence": 0.7})
    assert idiom_a == idiom_b == idiom_c


def test_normalize_timeline_preference():
    """Timeline and preference normalizers: drop empty, accept bare strings."""
    assert _normalize_timeline({"date": "2026-01-01", "event": "x"}) == {"date": "2026-01-01", "event": "x"}
    assert _normalize_timeline({"date": "", "event": "x"}) is None
    assert _normalize_timeline({"date": "2026-01-01", "event": ""}) is None
    assert _normalize_timeline(None) is None

    assert _normalize_preference("likes cats") == {"key": "preference", "value": "likes cats", "polarity": "like"}
    assert _normalize_preference({"key": "k", "value": "v"}) == {"key": "k", "value": "v", "polarity": "like"}
    assert _normalize_preference({"key": "k", "value": "v", "polarity": "dislike"}) == {"key": "k", "value": "v", "polarity": "dislike"}
    # Garbage polarity -> conservative "like"
    assert _normalize_preference({"key": "k", "value": "v", "polarity": "weird"})["polarity"] == "like"
    assert _normalize_preference({}) is None  # no value


def test_normalize_caps_lists_at_50():
    """Defensive cap at 50 entries per bucket."""
    facts = [{"entity": f"e{i}", "predicate": "p", "value": "v", "confidence": 0.5} for i in range(100)]
    out = _normalize({"facts": facts, "timeline": [], "preferences": []})
    assert len(out["facts"]) == 50


# ====== 8. JSON fence stripping ======

def test_extract_strips_json_fence():
    """LLMs often wrap JSON in ```json ... ``` -- strip it."""
    fenced = "```json\n{\"facts\": [], \"timeline\": [], \"preferences\": []}\n```"
    out = _strip_json_fence(fenced)
    parsed = json.loads(out)
    assert parsed == {"facts": [], "timeline": [], "preferences": []}

    # Bare JSON -> returned as-is
    bare = '{"facts": [], "timeline": [], "preferences": []}'
    assert _strip_json_fence(bare) == bare


# ====== 9. Source integrity (regression: not breaking __init__.py) ======

def test_source_module_attributes_present():
    """The extract module must expose the documented public surface."""
    assert hasattr(extract_mod, "extract")
    assert hasattr(extract_mod, "EXTRACT_SYSTEM")
    assert hasattr(extract_mod, "EMPTY_RESULT")
    assert callable(extract_mod.extract)
    assert "Cortex v6.5.0" in extract_mod.EXTRACT_SYSTEM
    assert extract_mod.EMPTY_RESULT == {"facts": [], "timeline": [], "preferences": []}


# ----- unittest wrapper so `python -m unittest server.tests.test_consolidate_v6_5` also works -----

class _AsyncToSyncWrapper:
    """Allow unittest.TestCase.run to invoke coroutine tests via asyncio.run."""

    _CORO_TESTS = [
        "test_extract_returns_valid_schema",
        "test_extract_handles_llm_unreachable",
        "test_extract_retries_on_failure",
        "test_extract_respects_timeout",
        "test_extract_facts_have_confidence",
        "test_extract_short_circuits_empty_content",
    ]

    def __init__(self, coro):
        self.coro = coro

    def run(self, *args, **kwargs):
        import asyncio
        return asyncio.run(self.coro)


def _wrap_async_methods(cls):
    """Decorator that re-points async test methods to asyncio.run-wrapped sync versions."""
    import inspect
    for name in list(cls.__dict__):
        if name.startswith("test_") and inspect.iscoroutinefunction(cls.__dict__[name]):
            original = cls.__dict__[name]
            def make(name, original):
                def sync_wrapper(self):
                    import asyncio
                    return asyncio.run(original(self))
                sync_wrapper.__name__ = name
                return sync_wrapper
            setattr(cls, name, make(name, original))
    return cls


# ---- A unittest-style mirror so `python -m unittest` works without pytest-asyncio hooks ----

@_wrap_async_methods
class TestExtractUnittestMirror(unittest.TestCase):
    """Unittest-flavored copy of the 6 async tests, so the file can be run
    either via ``pytest`` (preferred) or via ``python -m unittest``."""

    def test_extract_returns_valid_schema(self):
        async def _():
            llm_payload = {
                "choices": [{"message": {"content": json.dumps({
                    "facts": [{"entity": "Alice", "predicate": "age", "value": 30, "confidence": 0.9}],
                    "timeline": [], "preferences": [],
                })}}]
            }
            resp = _make_mock_response(llm_payload)
            mock_client = _make_mock_client(response=resp)
            cfg = _make_cfg()
            chunk = {"id": "c1", "content": "Alice is 30."}
            with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
                result = await extract(chunk, [], cfg)
            assert len(result["facts"]) == 1
        return _()

    def test_extract_handles_llm_unreachable(self):
        async def _():
            import httpx as _httpx
            mock_client = _make_mock_client(side_effect=_httpx.ConnectError("ECONNREFUSED"))
            cfg = _make_cfg(url="http://down/")
            chunk = {"id": "c1", "content": "x"}
            with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
                result = await extract(chunk, [], cfg)
            assert result == {"facts": [], "timeline": [], "preferences": []}
            assert mock_client.post.await_count >= 2
        return _()

    def test_extract_retries_on_failure(self):
        async def _():
            import httpx as _httpx
            timeout_exc = _httpx.TimeoutException("read timed out")
            ok = _make_mock_response({"choices": [{"message": {"content": json.dumps({
                "facts": [{"entity": "X", "predicate": "y", "value": "z", "confidence": 0.5}],
                "timeline": [], "preferences": [],
            })}}]})
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=[timeout_exc, ok])
            cfg = _make_cfg()
            chunk = {"id": "c1", "content": "x"}
            with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
                result = await extract(chunk, [], cfg)
            assert mock_client.post.await_count == 2
            assert result["facts"][0]["entity"] == "X"
        return _()

    def test_extract_respects_timeout(self):
        async def _():
            cfg = _make_cfg(timeout=42.0)
            captured = {}

            class FakeAsyncClient:
                def __init__(self, timeout=None, **kw):
                    captured["timeout"] = timeout

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return None

                async def post(self, url, json=None, headers=None):
                    return _make_mock_response({"choices": [{"message": {"content": "{}"}}]})

            chunk = {"id": "c1", "content": "x"}
            with patch("consolidate.extract.httpx.AsyncClient", FakeAsyncClient):
                await extract(chunk, [], cfg)
            assert captured["timeout"] == 42.0
        return _()

    def test_extract_facts_have_confidence(self):
        async def _():
            payload = {"choices": [{"message": {"content": json.dumps({
                "facts": [
                    {"entity": "A", "predicate": "p", "value": "v", "confidence": 0.5},
                    {"entity": "B", "predicate": "p", "value": "v", "confidence": 1.2},
                    {"entity": "C", "predicate": "p", "value": "v"},
                ],
                "timeline": [], "preferences": [],
            })}}]}
            resp = _make_mock_response(payload)
            mock_client = _make_mock_client(response=resp)
            cfg = _make_cfg()
            chunk = {"id": "c1", "content": "x"}
            with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
                result = await extract(chunk, [], cfg)
            assert len(result["facts"]) == 1
            assert result["facts"][0]["confidence"] == 0.5
        return _()

    def test_extract_short_circuits_empty_content(self):
        async def _():
            mock_client = _make_mock_client()
            cfg = _make_cfg()
            chunk = {"id": "c1", "content": ""}
            with patch("consolidate.extract.httpx.AsyncClient", return_value=mock_client):
                result = await extract(chunk, [], cfg)
            assert result == {"facts": [], "timeline": [], "preferences": []}
            assert mock_client.post.await_count == 0
        return _()


if __name__ == "__main__":
    unittest.main()
