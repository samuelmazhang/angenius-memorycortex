"""
UserPromptSubmit hook: recall from cortex before Claude answers.

Reads user prompt from stdin (Claude Code passes JSON), queries cortex,
and emits hookSpecificOutput.additionalContext on stdout so Claude sees
relevant memories before responding.

Patterns
--------
- "记住: <text>" / "remember: <text>"   → save <text> to cortex instead
- "what do you know" / "你知道" / "之前" → broader recall (limit=10, imp>=0)
- normal message                          → top-5 recall with imp>=4

Failure modes (ALL silent — never block the user):
- malformed stdin JSON        → return 0, no injection
- cortex unreachable / slow   → 2s timeout cap, swallowed
- empty results               → no injection
"""
from __future__ import annotations

import json
import os
import sys

# Make the package importable when the hook is invoked via absolute path.
_REPO_ROOT = os.getenv(
    "CORTEX_CONNECTOR_DIR",
    r".",
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from connector.claude import Cortex  # noqa: E402

HOOK_TIMEOUT = float(os.getenv("CORTEX_HOOK_TIMEOUT", "2.0"))
ENABLED = os.getenv("CORTEX_HOOK_ENABLED", "1") != "0"

_SAVE_PREFIXES = ("记住:", "记住：", "remember:", "remember：", "save:", "save：")
_BROAD_TOKENS = ("what do you know", "你知道", "之前", "previously", "earlier")


def _emit(additional: str | None) -> None:
    """Print the hook output JSON. additional=None → no injection."""
    if additional is None:
        # Still print a valid (empty) JSON so Claude Code parses cleanly.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}))
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional,
        }
    }))


def main() -> int:
    if not ENABLED:
        _emit(None)
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _emit(None)
        return 0

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        _emit(None)
        return 0

    try:
        cx = Cortex(timeout=HOOK_TIMEOUT)
    except Exception:
        _emit(None)
        return 0

    # ----- explicit save pattern -----
    for prefix in _SAVE_PREFIXES:
        if prompt.startswith(prefix):
            text = prompt[len(prefix):].strip()
            if not text:
                _emit(None)
                return 0
            try:
                rec = cx.remember(
                    text,
                    type="knowledge",
                    tags=["claude-hook", "explicit-save"],
                    source="claude-code-hook",
                    importance=6,
                )
                _emit(
                    f"[cortex] 已记住 → id={rec['id'][:24]}…  "
                    f"tier={rec['tier']}  importance={rec['importance']}  "
                    f"expires={rec['expires_at'][:10]}"
                )
            except Exception as e:
                _emit(f"[cortex] 记住失败: {type(e).__name__}: {str(e)[:80]}")
            return 0

    # ----- recall -----
    lower = prompt.lower()
    broad = any(tok in lower for tok in _BROAD_TOKENS)
    query = prompt[:200]  # cap query length

    try:
        if broad:
            hits = cx.recall(query, limit=10, min_importance=0, hybrid=True)
        else:
            hits = cx.recall(query, limit=5, min_importance=4, hybrid=True)
    except Exception:
        _emit(None)
        return 0

    if not hits:
        _emit(None)
        return 0

    # ----- format compact injection -----
    lines = [
        "<cortex_recall>",
        f"  query: {query[:60]}",
        f"  hits: {len(hits)} (mode={'broad' if broad else 'default'})",
    ]
    for i, h in enumerate(hits, 1):
        sim = float(h.get("similarity") or 0.0)
        snippet = (h.get("content") or "").replace("\n", " ")[:90]
        tags = ",".join(h.get("tags") or [])
        cid = h.get("id", "")[:18]
        lines.append(f"  [{i}] sim={sim:.2f} id={cid}… tags=[{tags}]")
        lines.append(f"      {snippet}…")
    lines.append("</cortex_recall>")
    _emit("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())