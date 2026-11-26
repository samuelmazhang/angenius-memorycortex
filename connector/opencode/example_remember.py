"""
example_remember.py — 简单的 remember/recall 用法示例.

用法:
    # 默认连 localhost:1880
    python connector/opencode/example_remember.py

    # 自定义 URL
    python connector/opencode/example_remember.py --base-url http://127.0.0.1:1880
"""

import argparse
import os
import sys

# 让脚本无论从哪里运行都能 import connector.opencode
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

try:
    from connector.opencode.opencode_cortex_loader import build_invoker
    from connector.opencode.memory_tool import MemoryTool, CortexError
except ImportError:
    # Standalone-mode import
    import importlib.util as ilu
    loader_spec = ilu.spec_from_file_location(
        'opencode_cortex_loader', os.path.join(_HERE, 'opencode_cortex_loader.py'))
    loader = ilu.module_from_spec(loader_spec); loader_spec.loader.exec_module(loader)
    tool_spec = ilu.spec_from_file_location(
        'memory_tool', os.path.join(_HERE, 'memory_tool.py'))
    tool = ilu.module_from_spec(tool_spec); tool_spec.loader.exec_module(tool)
    build_invoker = loader.build_invoker
    MemoryTool = tool.MemoryTool
    CortexError = tool.CortexError


def main():
    parser = argparse.ArgumentParser(description='Example: remember then recall')
    parser.add_argument('--base-url', default=os.environ.get('MEMORY_CORTEX_URL', 'http://localhost:1880'))
    parser.add_argument('--user-id', default='example-user')
    args = parser.parse_args()

    print(f'[example] using base_url={args.base_url}, user_id={args.user_id}')
    tool = MemoryTool(base_url=args.base_url)
    invoker = build_invoker(tool)

    try:
        # Step 1: remember
        print('\n[1] remember("I prefer Python for backend services")')
        chunk = invoker['remember'](
            content='I prefer Python for backend services',
            tags='python,backend,preference',
            importance=7,
            user_id=args.user_id,
        )
        print(f'    stored: id={chunk["id"]}')
        chunk_id = chunk['id']

        # Step 2: recall
        print('\n[2] recall("backend preference", top_k=3)')
        hits = invoker['recall'](
            query='backend preference',
            top_k=3,
            user_id=args.user_id,
        )
        if isinstance(hits, dict):
            hits = hits.get('results', [])
        print(f'    found {len(hits)} match(es)')
        for h in hits:
            print(f'      - {h["id"]}: {h.get("content", "")[:80]}')

        # Step 3: forget (cleanup)
        print(f'\n[3] forget(chunk_id="{chunk_id}")')
        result = invoker['forget'](chunk_id=chunk_id)
        print(f'    result: {result}')

        # Step 4: introspect
        print('\n[4] introspect()')
        info = invoker['introspect']()
        health = info.get('health', {})
        print(f'    health: {health.get("status", "unknown")} (version {health.get("version", "?")})')

        print('\n[done] example_remember.py green.')

    except CortexError as e:
        print(f'\n[ERROR] CortexError: {e} (status_code={e.status_code})')
        sys.exit(1)


if __name__ == '__main__':
    main()