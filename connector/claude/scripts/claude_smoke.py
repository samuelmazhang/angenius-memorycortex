"""
Standalone smoke runner — exercises every endpoint group without needing
Claude Code. Run with:

    cd .
    python -m connector.claude.scripts.claude_smoke

Or directly:

    python connector/claude/scripts/claude_smoke.py

Returns exit code 0 if no failures, 1 otherwise.
"""
from __future__ import annotations

import sys
import time

# Allow running both as a module and as a script.
try:
    from connector.claude import Cortex, CortexError
except ModuleNotFoundError:
    # Direct-script fallback: prepend repo root to sys.path.
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from connector.claude import Cortex, CortexError


def main() -> int:
    cx = Cortex()
    h = cx.health()
    print(f"[health] v={h.get('version')} chunks={h.get('chunks')} relations={h.get('relations')}")
    print(f"[mode]   {cx.mode()}")
    print()
    print("--- full self-test ---")

    t0 = time.time()
    r = cx.run_claude_smoke()
    dt = time.time() - t0

    print(f"\nOK   ({len(r['ok'])}): {r['ok']}")
    if r['warn']:
        print(f"\nWARN ({len(r['warn'])}):")
        for n, msg in r['warn']:
            print(f"  - {n}: {msg}")
    if r['fail']:
        print(f"\nFAIL ({len(r['fail'])}):")
        for n, msg in r['fail']:
            print(f"  - {n}: {msg}")
    print(f"\nelapsed: {dt:.1f}s")

    return 0 if not r['fail'] else 1


if __name__ == "__main__":
    sys.exit(main())