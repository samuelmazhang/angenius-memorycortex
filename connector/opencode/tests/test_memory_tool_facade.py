"""
TDD — connector/opencode/memory_tool.py facade.

策略: 用 monkeypatched client 验证 facade 行为 (输入校验 / 输出包装 / 错误传播).
不依赖真实 cortex.

覆盖 actions:
  remember(content, tags=..., importance=..., session_id=..., user_id=...) -> dict
  recall(query, top_k=..., user_id=..., session_id=...) -> list|dict
  forget(chunk_id=...) -> dict
  search(query, limit=..., tags=..., type=...) -> list  (raw POST /search)
  audit_query(action=..., limit=...) -> list
  introspect() -> dict
"""

import os
import sys
from unittest.mock import MagicMock

import importlib.util
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    'memory_tool',
    os.path.join(_HERE, '..', 'memory_tool.py'),
)
memory_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_tool)


def _fake_client():
    """Return a MagicMock client with sane return values."""
    client = MagicMock()
    client.remember.return_value = {'id': 'chunk-1', 'content': 'hi'}
    client.recall.return_value = [{'id': 'c1', 'content': 'match'}]
    client.forget.return_value = {'action': 'delete', 'chunk_id': 'c1'}
    client.introspect.return_value = {
        'health': {'status': 'ok', 'version': '5.3.9'},
        'stats': {'chunks': 100},
    }
    client._request.return_value = {'results': [], 'total': 0}  # default for raw search
    client.log_audit.return_value = {'ok': True}
    return client


# ============ CortexError ============

def test_cortex_error_attributes():
    e = memory_tool.CortexError('bad', status_code=500, body='oops')
    assert e.status_code == 500
    assert e.body == 'oops'
    assert 'bad' in str(e)


# ============ constructor / base_url ============

def test_default_base_url():
    t = memory_tool.MemoryTool()
    assert t.client.base_url == 'http://localhost:1880'


def test_custom_base_url():
    t = memory_tool.MemoryTool(base_url='http://127.0.0.1:1880')
    assert t.client.base_url == 'http://127.0.0.1:1880'


# ============ remember() facade ============

def test_remember_calls_client():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    result = t.remember('hello world')
    assert t.client.remember.called
    # remember() facade forwards content as kwarg
    assert t.client.remember.call_args.kwargs['content'] == 'hello world'
    assert result['id'] == 'chunk-1'


def test_remember_validates_content_empty():
    t = memory_tool.MemoryTool()
    try:
        t.remember('')
    except memory_tool.CortexError as e:
        assert 'empty' in str(e).lower()
        assert e.status_code == 400
        return
    assert False, "expected CortexError"


def test_remember_validates_importance_range():
    t = memory_tool.MemoryTool()
    try:
        t.remember('hi', importance=11)
    except memory_tool.CortexError as e:
        assert e.status_code == 400
        return
    assert False, "expected CortexError"


def test_remember_validates_importance_zero():
    t = memory_tool.MemoryTool()
    try:
        t.remember('hi', importance=0)
    except memory_tool.CortexError:
        return
    assert False, "expected CortexError"


def test_remember_propagates_client_error():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    t.client.remember.return_value = {'error': 'bad', 'status': 500}
    try:
        t.remember('hi')
    except memory_tool.CortexError as e:
        assert e.status_code == 500
        return
    assert False, "expected CortexError"


# ============ recall() facade ============

def test_recall_calls_client():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    result = t.recall('hello', top_k=3)
    assert t.client.recall.called
    assert result[0]['id'] == 'c1'


def test_recall_validates_top_k_positive():
    t = memory_tool.MemoryTool()
    try:
        t.recall('q', top_k=0)
    except memory_tool.CortexError:
        return
    assert False, "expected CortexError"


def test_recall_validates_top_k_max():
    t = memory_tool.MemoryTool()
    try:
        t.recall('q', top_k=100)
    except memory_tool.CortexError:
        return
    assert False, "expected top_k > 50 to fail"


# ============ forget() facade ============

def test_forget_by_chunk_id():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    result = t.forget(chunk_id='c1')
    assert result['action'] == 'delete'
    assert t.client.forget.called


def test_forget_no_args_raises():
    t = memory_tool.MemoryTool()
    try:
        t.forget()
    except memory_tool.CortexError as e:
        assert e.status_code == 400
        return
    assert False, "expected CortexError"


# ============ search() raw wrapper ============

def test_search_uses_raw_request():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    t.search('python', limit=5)
    assert t.client._request.called
    args, kwargs = t.client._request.call_args
    assert args[0] == 'POST'
    assert args[1] == '/search'
    body = kwargs.get('body') or args[2]
    assert body['query'] == 'python'
    assert body['limit'] == 5


def test_search_default_limit():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    t.search('q')
    _, kwargs = t.client._request.call_args
    assert kwargs['body']['limit'] == 10  # default


# ============ audit_query() ============

def test_audit_query_uses_log_audit():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    t.audit_query(action='recall', limit=20)
    assert t.client._request.called
    args, kwargs = t.client._request.call_args
    assert args[0] == 'POST'
    assert args[1] == '/audit/query'
    body = kwargs.get('body') or args[2]
    assert body['action'] == 'recall'
    assert body['limit'] == 20


# ============ introspect() ============

def test_introspect_returns_client_introspect():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    result = t.introspect()
    assert result['health']['status'] == 'ok'
    assert result['stats']['chunks'] == 100


# ============ batch operations ============

def test_batch_operations_writes_in_sequence():
    """batch(operations=[...]) 应该串行调用 remember."""
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    t.client.remember.side_effect = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
    results = t.batch([{'action': 'remember', 'content': 'a'},
                      {'action': 'remember', 'content': 'b'},
                      {'action': 'remember', 'content': 'c'}])
    ids = [r['id'] for r in results]
    assert ids == ['a', 'b', 'c']
    assert t.client.remember.call_count == 3


def test_batch_supports_mixed_actions():
    t = memory_tool.MemoryTool()
    t.client = _fake_client()
    results = t.batch([{'action': 'remember', 'content': 'x'},
                      {'action': 'forget', 'chunk_id': 'y'}])
    assert t.client.remember.called
    assert t.client.forget.called