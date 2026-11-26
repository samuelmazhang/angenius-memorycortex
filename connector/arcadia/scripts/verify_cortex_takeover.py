"""Verify Patch 05 — `memory_tool(action='add')` actually reaches cortex when
CORTEX_TAKEOVER=1 and the in-memory snapshot is already at the 2,200-char limit.

Before Patch 05: `memory_tool` returns "Memory at 18,175/2,200 chars" and the
chunk never lands in cortex.
After Patch 05: `memory_tool` returns {"success": true, "entry_count": 28,
"message": "Entry added."} and cortex server's `/health.chunks` increments
by 1.

Run from the arcadia venv:

    /a/Application/Arcadia-Agent/Program/arcadia-agent/venv/Scripts/python.exe \\
        ".connector\\arcadia\\scripts\\verify_cortex_takeover.py"

Exit codes:
    0 = fix in place (write reaches cortex)
    1 = fix missing (write blocked by gate)
    2 = script / env error
"""
import json
import os
import sys
import urllib.request

ARCADIA_REPO = r"A:/Application/Arcadia-Agent/Program/arcadia-agent"
if ARCADIA_REPO not in sys.path:
    sys.path.insert(0, ARCADIA_REPO)
os.chdir(ARCADIA_REPO)


def get_cortex_chunks() -> int:
    data = json.loads(
        urllib.request.urlopen("http://127.0.0.1:1880/health", timeout=5).read()
    )
    return int(data.get("chunks", 0))


def main() -> int:
    # 1. baseline count
    before = get_cortex_chunks()

    # 2. full memory_tool dispatcher call (NOT bypassed backend.create_entry)
    from tools.memory_tool import memory_tool, MemoryStore

    store = MemoryStore(memory_char_limit=2200, user_char_limit=1375, use_cortex=True)
    store.load_from_disk()
    print(f"  use_cortex: {store.use_cortex}")
    print(f"  in-memory snapshot: {store._char_count('memory'):,} chars "
          f"(limit {store._char_limit('memory'):,})")
    print(f"  cortex chunks before: {before}")

    # 3. fire a real `add` through the dispatcher
    content = (
        "Patch-05 verification probe at "
        f"2026-09-17 — confirming memory_tool(action='add') "
        "now writes through to cortex instead of being blocked by the "
        "local-file parity gate. "
    )
    result_str = memory_tool(action="add", content=content, store=store, target="memory")
    result = json.loads(result_str)
    print(f"\n  memory_tool result: {result}")

    if not result.get("success"):
        print("FAIL: write was rejected — Patch 05 not in place (still blocked by gate).")
        return 1

    # 4. confirm cortex received it
    import time
    time.sleep(1)
    after = get_cortex_chunks()
    print(f"  cortex chunks after:  {after}")
    delta = after - before
    print(f"  delta: {delta:+d}")

    if delta < 1:
        print("FAIL: cortex count did not increment. Patch 05 partial — "
              "dispatcher returned success but cortex never received the chunk.")
        return 1

    # 5. confirm audit shows our write
    audit = json.loads(
        urllib.request.urlopen("http://127.0.0.1:1880/audit?limit=3", timeout=5).read()
    )
    print("\n  audit latest 3:")
    for e in audit["entries"][:3]:
        actor = e.get("details", {}).get("actor", "?")
        ts = e.get("created_at", "")[:19]
        print(f"    {ts} {e['action']:8s} actor={actor[:35]}")

    print("\nPASS: Patch 05 in place — memory_tool writes reach cortex end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())