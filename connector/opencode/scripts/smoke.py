"""
CLI smoke test — 验证 cortex server (或 mock) 至少 5 个 action 端到端工作.

用法:
    # vs real cortex server (default localhost:1880)
    python connector/opencode/scripts/smoke.py

    # vs custom URL
    python connector/opencode/scripts/smoke.py --base-url http://127.0.0.1:1880

    # mock mode (no server needed, 用 in-memory backend)
    python connector/opencode/scripts/smoke.py --mock

exit code 0 = 全绿, 1 = 失败.
"""

import argparse
import os
import sys
import json
import urllib.error
from io import BytesIO


def _import_tool():
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)  # connector/opencode/
    import importlib.util as ilu
    spec = ilu.spec_from_file_location('memory_tool', os.path.join(parent, 'memory_tool.py'))
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mock_urlopen_factory():
    """Return (mock_urlopen, backend) pair — in-memory cortex emulation."""
    backend = {'chunks': {}, 'next_id': 1, 'audit': []}

    def urlopen(req, timeout=None):
        url = req.full_url
        method = req.method
        body = json.loads(req.data) if req.data else None
        path = url.split('://', 1)[1].split('/', 1)[1].split('?')[0]

        def resp(status, body_obj):
            class R:
                headers = {'Content-Type': 'application/json'}
                def read(self_inner): return json.dumps(body_obj).encode()
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            if status >= 400:
                raise urllib.error.HTTPError('mock://x', status, 'err', {}, BytesIO(json.dumps(body_obj).encode()))
            return R()

        if path == 'health':
            return resp(200, {'status': 'ok', 'version': '5.3.9'})
        if path == 'search' and method == 'POST':
            q = body.get('query', '')
            results = [c for c in backend['chunks'].values()
                       if q in c.get('content', '')]
            return resp(200, {'total': len(results), 'results': results[:body.get('limit', 10)]})
        if path == 'chunks' and method == 'POST':
            cid = f"chunk-{backend['next_id']}"
            backend['next_id'] += 1
            chunk = {'id': cid, 'content': body['content'],
                     'type': body.get('type', 'knowledge'),
                     'tags': body.get('tags', []),
                     'importance': body.get('importance', 5)}
            backend['chunks'][cid] = chunk
            return resp(200, chunk)
        if path.startswith('chunks/') and method == 'DELETE':
            cid = path.split('/')[1]
            if cid in backend['chunks']:
                del backend['chunks'][cid]
            return resp(200, {'ok': True})
        if path == 'working/set' and method == 'POST':
            return resp(200, {'ok': True})
        if path == 'consolidate/extract' and method == 'POST':
            return resp(200, {'ok': True})
        if path == 'audit/log' and method == 'POST':
            backend['audit'].append(body)
            return resp(200, {'ok': True})
        if path == 'audit/query' and method == 'POST':
            return resp(200, {'results': backend['audit'][:body.get('limit', 50)], 'total': len(backend['audit'])})
        if path == 'stats' and method == 'GET':
            return resp(200, {'chunks': len(backend['chunks'])})
        if path == 'stats/ops' and method == 'GET':
            return resp(200, {'writes': len(backend['audit'])})
        return resp(404, {'detail': 'not found', 'path': path})

    return urlopen, backend


def main():
    parser = argparse.ArgumentParser(description='Cortex connector smoke')
    parser.add_argument('--base-url', default=os.environ.get('MEMORY_CORTEX_URL', 'http://localhost:1880'),
                        help='Cortex server base URL')
    parser.add_argument('--mock', action='store_true', help='Use in-memory mock backend (no server needed)')
    args = parser.parse_args()

    tool_mod = _import_tool()
    tool = tool_mod.MemoryTool(base_url=args.base_url)

    if args.mock:
        urlopen_fn, _ = mock_urlopen_factory()
        # Patch the underlying module's urlopen
        import importlib.util as ilu
        here = os.path.dirname(os.path.abspath(__file__))
        cc_spec = ilu.spec_from_file_location('cortex_client', os.path.join(os.path.dirname(here), 'cortex_client.py'))
        cc_mod = ilu.module_from_spec(cc_spec)
        cc_spec.loader.exec_module(cc_mod)
        orig_urlopen = cc_mod.urllib.request.urlopen
        cc_mod.urllib.request.urlopen = urlopen_fn
        print('[smoke] mock mode (no real cortex needed)')
    else:
        cc_mod = None
        orig_urlopen = None

    failures = []

    def check(name, fn):
        try:
            r = fn()
            print(f'  ✓ {name}')
            return r
        except Exception as e:
            print(f'  ✗ {name}: {e}')
            failures.append(name)

    print('\n[1/6] introspect')
    check('introspect', lambda: tool.introspect())

    print('\n[2/6] remember')
    chunk_id = None
    def do_remember():
        nonlocal chunk_id
        c = tool.remember('smoke test content', tags='smoke', importance=5)
        chunk_id = c['id']
        return c
    check('remember', do_remember)

    print('\n[3/6] recall')
    check('recall', lambda: tool.recall('smoke', top_k=3))

    print('\n[4/6] forget')
    if chunk_id:
        check('forget', lambda: tool.forget(chunk_id=chunk_id))

    print('\n[5/6] batch')
    check('batch remember', lambda: tool.batch([
        {'action': 'remember', 'content': 'batch-1'},
        {'action': 'remember', 'content': 'batch-2'},
    ]))

    print('\n[6/6] search (raw)')
    check('search', lambda: tool.search('smoke', limit=5))

    # Restore
    if args.mock and cc_mod:
        cc_mod.urllib.request.urlopen = orig_urlopen

    print(f'\n--- {"GREEN" if not failures else f"RED ({len(failures)} failed)"} ---')
    if failures:
        print(f'Failed: {failures}')
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()