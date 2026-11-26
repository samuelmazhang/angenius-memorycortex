"""
SessionEnd hook: persist a session-closure marker to cortex.

Reads session info from stdin (Claude Code passes {session_id, reason,
transcript_path?}). Saves a small "session_summary" chunk so cortex knows
the session ended — useful for time-window recall and audit trails.

Does NOT summarize the full transcript (that's a future iteration when
we hook a transcript summarizer). Just records the closure.

Failure modes: ALL silent — never block Claude shutdown.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

_REPO_ROOT = os.getenv(
    "CORTEX_CONNECTOR_DIR",
    r".",
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from connector.claude import Cortex  # noqa: E402

HOOK_TIMEOUT = float(os.getenv("CORTEX_HOOK_TIMEOUT", "3.0"))
ENABLED = os.getenv("CORTEX_HOOK_ENABLED", "1") != "0"


def main() -> int:
    if not ENABLED:
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    session_id = (payload.get("session_id") or "unknown")[:48]
    reason = (payload.get("reason") or "unknown")[:32]
    transcript_path = payload.get("transcript_path") or ""

    summary = (
        f"Claude Code session ended. "
        f"reason={reason} "
        f"at={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} "
        f"transcript={transcript_path}"
    ).strip()

    try:
        cx = Cortex(timeout=HOOK_TIMEOUT)
        rec = cx.remember(
            summary,
            type="session_summary",
            tags=["claude-hook", "session-end", session_id],
            source="claude-code-hook",
            importance=4,
        )
        # Side-band diagnostic so logs show what happened (not injected
        # into Claude — this hook runs after the session, no UI to inject).
        print(json.dumps({
            "status": "saved",
            "id": rec.get("id"),
            "tier": rec.get("tier"),
        }))
    except Exception as e:
        print(json.dumps({
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:80]}",
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())