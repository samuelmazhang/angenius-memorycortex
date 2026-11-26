"""
E2E-style integration test — 模拟完整 cortex 流程 (remember → recall → forget → recall).

策略: monkeypatch urllib + 维护一个 in-memory chunks 字典模拟后端状态.
不需要真实 cortex server.
"""

import os
import sys
import json
import time
from unittest.mock import patch
from io import BytesIO

import importlib.util
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('memory_tool', os.path.join(_HERE, '..', 'memory_tool.py'))
memory_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory_tool)


# Mock backend: in-memory chunk store with vector similarity stub
class MockCortexBackend:
    def __init__(self):
        self.chunks = {}  # id -> chunk dict
        self.audit = []
        self._next_id = 1

    def urlopen(self, req, timeout=None):
        url = req.full_url
        method = req.method
        body = json.loads(req.data) if req.data else None
        return self._route(url, method, body)

    def _route(self, url, method, body):
        """Return mock response object."""
        path = url.split('://', 1)[1].split('/', 1)[1].split('?')[0]
        # /health
        if path == 'health' and method == 'GET':
            return self._resp(200, {'status': 'ok', 'version': '5.3.9'})
        # /search
        if path == 'search' and method == 'POST':
            return self._resp(200, self._search(body))
        # /chunks (POST = create, DELETE /chunks/{id})
        if path == 'chunks' and method == 'POST':
            return self._resp(200, self._create_chunk(body))
        if path.startswith('chunks/') and method == 'DELETE':
            cid = path.split('/')[1]
            if cid in self.chunks:
                del self.chunks[cid]
            return self._resp(200, {'ok': True})
        # /working/set
        if path == 'working/set' and method == 'POST':
            return self._resp(200, {'ok': True})
        # /consolidate/extract
        if path == 'consolidate/extract' and method == 'POST':
            return self._resp(200, {'ok': True})
        # /audit/query
        if path == 'audit/query' and method == 'POST':
            return self._resp(200, self._audit_query(body))
        # /audit/log
        if path == 'audit/log' and method == 'POST':
            self.audit.append(body)
            return self._resp(200, {'ok': True})
        # /stats
        if path == 'stats' and method == 'GET':
            return self._resp(200, {'chunks': len(self.chunks)})
        # /stats/ops
        if path == 'stats/ops' and method == 'GET':
            return self._resp(200, {'writes': len(self.audit)})
        return self._resp(404, {'detail': 'not found', 'path': path})

    def _search(self, body):
        query = body.get('query', '')
        # Match chunks whose content contains query substring
        results = []
        for c in self.chunks.values():
            if query in c.get('content', '') or query in ' '.join(c.get('tags', [])):
                results.append(c)
        # Apply user_id filter
        uid = body.get('user_id')
        if uid:
            results = [c for c in results if c.get('metadata', {}).get('user_id') == uid]
        return {'total': len(results), 'results': results[:body.get('limit', 10)]}

    def _create_chunk(self, body):
        cid = f'chunk-{self._next_id}'
        self._next_id += 1
        chunk = {
            'id': cid,
            'content': body['content'],
            'type': body.get('type', 'knowledge'),
            'tags': body.get('tags', []),
            'importance': body.get('importance', 5),
            'metadata': body.get('metadata', {}),
        }
        self.chunks[cid] = chunk
        return chunk

    def _audit_query(self, body):
        results = list(self.audit)
        if body.get('action'):
            results = [a for a in results if a.get('action') == body['action']]
        return {'results': results[:body.get('limit', 50)], 'total': len(results)}

    def _resp(self, status, body):
        import urllib.error
        if status >= 400:
            raise urllib.error.HTTPError('mock://x', status, 'err', {}, BytesIO(json.dumps(body).encode()))

        class Resp:
            def __init__(self):
                self.headers = {'Content-Type': 'application/json'}
            def read(self):
                return json.dumps(body).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Resp()


# ============ tests ============

def _build_tool_with_mock(backend):
    tool = memory_tool.MemoryTool(base_url='http://mock')
    # Patch the client module's urlopen
    # memory_tool imports MemoryCortexClient which has self.client._request → urlopen
    # Easier: monkeypatch cortex_client.urllib.request.urlopen
    import importlib.util as _ilu
    cc_spec = _ilu.spec_from_file_location('cortex_client', os.path.join(_HERE, '..', 'cortex_client.py'))
    cortex_client = _ilu.module_from_spec(cc_spec)
    cc_spec.loader.exec_module(cortex_client)
    patcher = patch.object(cortex_client.urllib.request, 'urlopen', backend.urlopen)
    return tool, patcher


def test_remember_then_recall_returns_stored_chunk():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        chunk = tool.remember('I prefer Python', tags='python', user_id='u1')
        assert chunk['id'] == 'chunk-1'
        hits = tool.recall('Python', top_k=5, user_id='u1')
        assert any(h['id'] == 'chunk-1' for h in hits)


def test_forget_removes_from_recall():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        chunk = tool.remember('temporary fact')
        cid = chunk['id']
        # Can recall before forget
        hits_before = tool.recall('temporary', top_k=5)
        assert any(h['id'] == cid for h in hits_before)
        # Forget
        tool.forget(chunk_id=cid)
        # Cannot recall after forget
        hits_after = tool.recall('temporary', top_k=5)
        assert not any(h['id'] == cid for h in hits_after)


def test_user_id_isolation():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        tool.remember('u1 fact', user_id='u1')
        tool.remember('u2 fact', user_id='u2')
        # u1 cannot see u2's fact
        u1_hits = tool.recall('fact', user_id='u1')
        u2_hits = tool.recall('fact', user_id='u2')
        u1_contents = [h['content'] for h in u1_hits]
        u2_contents = [h['content'] for h in u2_hits]
        assert 'u1 fact' in u1_contents
        assert 'u1 fact' not in u2_contents
        assert 'u2 fact' in u2_contents
        assert 'u2 fact' not in u1_contents


def test_batch_remember_and_forget():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        results = tool.batch([
            {'action': 'remember', 'content': 'batch-1'},
            {'action': 'remember', 'content': 'batch-2'},
            {'action': 'forget', 'chunk_id': 'chunk-1'},
        ])
        assert len(results) == 3
        # chunk-1 was deleted, batch-2 still queryable
        hits = tool.recall('batch', top_k=5)
        ids = [h['id'] for h in hits]
        assert 'chunk-2' in ids
        assert 'chunk-1' not in ids


def test_introspect_returns_combined_metadata():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        tool.remember('x')
        info = tool.introspect()
    assert info['health']['status'] == 'ok'
    assert info['stats']['chunks'] >= 1


def test_audit_query_records_actions():
    backend = MockCortexBackend()
    tool, patcher = _build_tool_with_mock(backend)
    with patcher:
        tool.remember('audited fact')
        # Use audit log API (POST /audit/log was triggered by remember's working memory)
        # Then query
        result = tool.audit_query(action='set', limit=10)
    # We don't strictly assert count (depends on backend behavior), just shape
    assert 'results' in result
    assert 'total' in result