"""
Demo smoke — 用 opencode cortex loader 调用 cortex server 验证 3 个 action.

用法:
    python connector/opencode/demo_smoke.py [--base-url URL] [--user-id ID]

默认连 http://localhost:1880, 设置 MEMORY_CORTEX_URL 可覆盖.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    from connector.opencode.opencode_cortex_loader import build_invoker
    from connector.opencode.memory_tool import MemoryTool
except ImportError:
    # Standalone-mode import (script run from anywhere)
    import importlib.util as _ilu
    _loader_spec = _ilu.spec_from_file_location(
        'opencode_cortex_loader', os.path.join(_HERE, 'opencode_cortex_loader.py'))
    _loader = _ilu.module_from_spec(_loader_spec)
    _loader_spec.loader.exec_module(_loader)
    _tool_spec = _ilu.spec_from_file_location(
        'memory_tool', os.path.join(_HERE, 'memory_tool.py'))
    _tool = _ilu.module_from_spec(_tool_spec)
    _tool_spec.loader.exec_module(_tool)
    build_invoker = _loader.build_invoker
    MemoryTool = _tool.MemoryTool


def main():
    parser = argparse.ArgumentParser(description='Cortex memory demo smoke')
    parser.add_argument('--base-url', default=os.environ.get('MEMORY_CORTEX_URL', 'http://localhost:1880'),
                        help='Cortex server base URL')
    parser.add_argument('--user-id', default='demo-opencode',
                        help='user_id for cross-user isolation')
    parser.add_argument('--quiet', action='store_true',
                        help='less output')
    args = parser.parse_args()

    print(f'[demo_smoke] connecting to {args.base_url} as user={args.user_id}')
    tool = MemoryTool(base_url=args.base_url)
    invoker = build_invoker(tool)

    # 1. Introspect
    print('\n[1/3] introspect()')
    info = invoker['introspect']()
    if not args.quiet:
        print(f'  health: {info.get("health")}')
        print(f'  stats: {info.get("stats")}')

    # 2. Remember
    content = f'demo_smoke test at {os.environ.get("COMPUTERNAME", "unknown")}'
    print(f'\n[2/3] remember(content={content!r})')
    chunk = invoker['remember'](
        content=content,
        tags='demo,smoke,opencode',
        importance=5,
        user_id=args.user_id,
    )
    chunk_id = chunk.get('id')
    print(f'  stored: {chunk_id}')

    # 3. Recall + Forget
    print('\n[3/3] recall(query="demo")')
    hits = invoker['recall'](query='demo', top_k=3, user_id=args.user_id)
    if isinstance(hits, dict):
        hits = hits.get('results', [])
    print(f'  found {len(hits)} hit(s)')
    for h in hits[:3]:
        print(f"    - {h.get('id')}: {h.get('content', '')[:80]}")

    if chunk_id:
        print(f'\n[cleanup] forget(chunk_id={chunk_id})')
        res = invoker['forget'](chunk_id=chunk_id)
        print(f'  result: {res}')

    print('\n[done] demo_smoke green.')


if __name__ == '__main__':
    main()