"""
TDD 测试 — connector/opencode/cortex_client.py

策略: 用 monkeypatch 替换 urllib.request.urlopen, 让测试不依赖真实 cortex server,
但精确验证 client 的请求路径/方法/header/body 序列化。

这些测试覆盖:
- _request() 低阶 helper (URL 拼接 / JSON / None params / error handling)
- remember() → POST /chunks (+ session_id working memory + extract cascade)
- recall() → POST /search (+ user_id 客户端过滤)
- forget() → DELETE /chunks/{id} | TTL | bulk
- reason() → POST /search/hybrid + /search + /hierarchy/enrich + /links
- introspect() → GET /health + /stats + /stats/ops
"""

import json
import sys
import os
import urllib.error
from unittest.mock import patch, MagicMock
from io import BytesIO

# 让测试可以导入 connector/opencode/cortex_client.py
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, '..', '..', '..')))

# 直接以路径方式 import (避免被外层 __init__.py 当作 package 拉进来)
import importlib.util
_spec = importlib.util.spec_from_file_location(
    'cortex_client',
    os.path.join(_HERE, '..', 'cortex_client.py'),
)
cortex_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cortex_client)


class CaseInsensitiveHeaders:
    """Simulates urllib HTTPMessage's case-insensitive header lookup."""
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, key, default=None):
        return self._d.get(key.lower(), default)

    def __getitem__(self, key):
        return self._d[key.lower()]

    def __iter__(self):
        return iter(self._d)

    def items(self):
        return list(self._d.items())


def _make_response(body, status=200, content_type=None):
    """构造一个 urllib response 的 mock."""
    resp = MagicMock()
    if isinstance(body, str):
        raw_bytes = body.encode('utf-8')
    else:
        raw_bytes = body
    resp.read.return_value = raw_bytes
    # Auto-detect content-type from body if not provided
    if content_type is None:
        stripped = raw_bytes.lstrip()
        if stripped.startswith((b'{', b'[')):
            content_type = 'application/json'
        else:
            content_type = 'text/plain'
    resp.headers = CaseInsensitiveHeaders({'Content-type': content_type})
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _patch_urlopen(responses_by_url):
    """
    responses_by_url: list of response objects, OR list of ('raise', exc) tuples.
    Each item is consumed in sequence; subsequent urlopen calls raise IndexError.
    """
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append({
            'url': req.full_url,
            'method': req.method,
            'headers': dict(req.headers),
            'body': req.data.decode('utf-8') if req.data else None,
            'timeout': timeout,
        })
        if isinstance(responses_by_url, list):
            idx = len(calls) - 1
            if idx < len(responses_by_url):
                item = responses_by_url[idx]
                if isinstance(item, tuple) and item[0] == 'raise':
                    raise item[1]
                return item
            raise IndexError("More calls than mocked responses")
        return responses_by_url(calls)

    return fake_urlopen, calls


# ============ _request() 测试 ============

def test_request_get_url_construction():
    """GET 请求 URL 拼接 (无 params)."""
    fake, calls = _patch_urlopen([_make_response('ok')])
    client = cortex_client.MemoryCortexClient('http://localhost:1880/')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client._request('GET', '/health')
    assert calls[0]['url'] == 'http://localhost:1880/health'
    assert calls[0]['method'] == 'GET'
    assert result == 'ok'


def test_request_strips_none_params():
    """None params 必须被 strip (server 用 string 'None' 会查 0 行)."""
    fake, calls = _patch_urlopen([_make_response('{}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client._request('GET', '/chunks', params={'tag': 'x', 'skip': None, 'limit': 5})
    # None 应该被 strip, URL 应该只包含 tag + limit
    assert 'tag=x' in calls[0]['url']
    assert 'limit=5' in calls[0]['url']
    assert 'None' not in calls[0]['url']


def test_request_post_json_body():
    """POST 请求序列化 body 为 JSON."""
    fake, calls = _patch_urlopen([_make_response('{"id":"c1"}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client._request('POST', '/chunks', body={'content': 'hello'})
    assert calls[0]['method'] == 'POST'
    # urllib normalizes header names via capwords (Content-type not Content-Type)
    headers_lower = {k.lower(): v for k, v in calls[0]['headers'].items()}
    assert headers_lower['content-type'] == 'application/json'
    body = json.loads(calls[0]['body'])
    assert body == {'content': 'hello'}
    assert result == {'id': 'c1'}


def test_request_http_error_returns_error_dict():
    """HTTPError 不应该 raise, 应该返回 error dict."""
    fake, calls = _patch_urlopen([('raise', urllib.error.HTTPError(
        'http://h:1880/chunks/c1', 404, 'Not Found', {}, BytesIO(b'{"detail":"not found"}')
    ))])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client._request('DELETE', '/chunks/c1')
    assert isinstance(result, dict), f"expected dict, got {type(result).__name__}: {result!r}"
    assert result.get('status') == 404
    assert 'error' in result


# ============ remember() 测试 ============

def test_remember_basic():
    """remember 调用 POST /chunks."""
    fake, calls = _patch_urlopen([_make_response('{"id":"chunk-1","content":"hi"}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.remember('hello world')
    assert calls[0]['url'].endswith('/chunks')
    assert calls[0]['method'] == 'POST'
    body = json.loads(calls[0]['body'])
    assert body['content'] == 'hello world'
    assert body['importance'] == 5  # default
    assert result['id'] == 'chunk-1'


def test_remember_with_session_id_triggers_working_memory():
    """remember + session_id 应该额外 POST /working/set."""
    fake, calls = _patch_urlopen([
        _make_response('{"id":"chunk-2"}'),
        _make_response('{"ok":true}'),
    ])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.remember('hello', session_id='sess-1')
    assert len(calls) == 2
    assert '/working/set' in calls[1]['url']
    body = json.loads(calls[1]['body'])
    assert body['session_id'] == 'sess-1'


def test_remember_coerces_tags_string():
    """tags 为 string 'a,b,c' 应当被 split 为 list."""
    fake, calls = _patch_urlopen([_make_response('{"id":"x"}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        client.remember('hi', tags='a, b ,c')
    body = json.loads(calls[0]['body'])
    assert body['tags'] == ['a', 'b', 'c']


# ============ recall() 测试 ============

def test_recall_basic():
    """recall 调用 POST /search."""
    fake, calls = _patch_urlopen([_make_response('{"results":[{"id":"c1","content":"hi"}],"total":1}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.recall('hello', top_k=3)
    assert '/search' in calls[0]['url']
    body = json.loads(calls[0]['body'])
    assert body['query'] == 'hello'
    assert body['limit'] == 3
    assert result[0]['id'] == 'c1'


def test_recall_user_id_filter():
    """recall + user_id 应该客户端过滤 metadata.user_id."""
    payload = {
            'results': [
                {'id': 'a', 'content': 'a', 'metadata': {'user_id': 'u1'}},
                {'id': 'b', 'content': 'b', 'metadata': {'user_id': 'u2'}},
                {'id': 'c', 'content': 'c', 'metadata': {'user_id': 'u1'}},
            ],
            'total': 3,
        }
    fake, calls = _patch_urlopen([_make_response(json.dumps(payload))])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.recall('q', top_k=2, user_id='u1')
    # recall() 后端解包 {total, results}, 返回 list of hits
    assert isinstance(result, list), f"expected list, got {type(result).__name__}"
    ids = [r['id'] for r in result]
    assert ids == ['a', 'c']


# ============ forget() 测试 ============

def test_forget_by_id():
    """forget(chunk_id) → DELETE /chunks/{id}."""
    fake, calls = _patch_urlopen([_make_response('{"ok":true}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.forget(chunk_id='c1')
    assert calls[0]['method'] == 'DELETE'
    assert calls[0]['url'].endswith('/chunks/c1')
    assert result['action'] == 'delete'


def test_forget_ttl():
    """forget(expire_old_days=30) → POST /ttl/archive."""
    fake, calls = _patch_urlopen([_make_response('{"archived":5}')])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.forget(expire_old_days=30)
    assert '/ttl/archive' in calls[0]['url']
    assert result['action'] == 'ttl_archive'


def test_forget_no_args_returns_error():
    """无参数应当返回 error dict."""
    client = cortex_client.MemoryCortexClient('http://h:1880')
    result = client.forget()
    assert 'error' in result


# ============ reason() 测试 ============

def test_reason_calls_hybrid_and_search_and_enrich():
    """reason 调用 /search/hybrid + /search + /hierarchy/enrich."""
    fake, calls = _patch_urlopen([
        _make_response('{"rankings_by_strategy":{"vector":[],"fts5":[]}}'),  # /search/hybrid
        _make_response('{"results":[],"total":0}'),                            # /search
    ])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.reason('why?', top_k=2)
    assert len(calls) == 2
    assert '/search/hybrid' in calls[0]['url']
    assert calls[1]['url'].endswith('/search')
    assert result['question'] == 'why?'
    assert 'context_chunks' in result


# ============ introspect() 测试 ============

def test_introspect_returns_combined_metadata():
    """introspect() → /health + /stats + /stats/ops."""
    fake, calls = _patch_urlopen([
        _make_response('{"status":"ok","version":"5.3.9"}'),  # /health
        _make_response('{"chunks":100}'),                       # /stats
        _make_response('{"writes":50}'),                        # /stats/ops
    ])
    client = cortex_client.MemoryCortexClient('http://h:1880')
    with patch.object(cortex_client.urllib.request, 'urlopen', fake):
        result = client.introspect()
    assert len(calls) == 3
    assert calls[0]['url'].endswith('/health')
    assert calls[1]['url'].endswith('/stats')
    assert calls[2]['url'].endswith('/stats/ops')
    assert result['health']['version'] == '5.3.9'


# ============ constructor / base_url ============

def test_base_url_trailing_slash_stripped():
    client = cortex_client.MemoryCortexClient('http://h:1880/')
    assert client.base_url == 'http://h:1880'


def test_default_base_url_localhost():
    client = cortex_client.MemoryCortexClient()
    assert client.base_url == 'http://localhost:1880'