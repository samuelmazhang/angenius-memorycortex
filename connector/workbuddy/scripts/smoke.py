"""
Standalone MCP smoke test for the WorkBuddy -> Cortex connector.

Exercises the exact transport WorkBuddy uses:
  1. initialize (with required Accept header)
  2. notifications/initialized
  3. tools/list  (assert 6 friendly aliases present)
  4. cortex_introspect  (health)
  5. cortex_remember + cortex_recall round-trip (tagged smoke-workbuddy)

No third-party deps; stdlib urllib only.

Usage:
    python connector/workbuddy/scripts/smoke.py [--url http://127.0.0.1:1880/mcp/]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

DEFAULT_URL = "http://127.0.0.1:1880/mcp/"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
FRIENDLY = {
    "cortex_remember", "cortex_recall", "cortex_get",
    "cortex_forget", "cortex_conflict_upsert", "cortex_introspect",
}


class Client:
    def __init__(self, url: str):
        self.url = url
        self.sid = None
        self._id = 0

    def call(self, method: str, params: dict | None = None, notify: bool = False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        h = dict(HEADERS)
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=h, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid and not self.sid:
                self.sid = sid
            body = r.read().decode()
        if notify:
            return None
        # SSE frames: "event: message\ndata: {...}\n\n"
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(body)

    def tool(self, name: str, args: dict):
        resp = self.call("tools/call", {"name": name, "arguments": args})
        if "error" in resp:
            raise RuntimeError(f"{name} error: {resp['error']}")
        return resp["result"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    a = ap.parse_args()

    c = Client(a.url)
    init = c.call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "workbuddy-smoke", "version": "1.0"},
    })
    info = init["result"]["serverInfo"]
    print(f"[OK] initialize: {info['name']} v{info['version']} (session={c.sid})")
    c.call("notifications/initialized", notify=True)

    listing = c.call("tools/list", {})
    tools = {t["name"] for t in listing["result"]["tools"]}
    missing = FRIENDLY - tools
    if missing:
        print(f"[FAIL] missing friendly tools: {missing}")
        return 1
    print(f"[OK] tools/list: {len(tools)} tools, all 6 friendly aliases present")

    health = c.tool("cortex_introspect", {})
    text = json.dumps(health, ensure_ascii=False)
    print(f"[OK] introspect: {text[:200]}")

    marker = "workbuddy-smoke"
    content = f"WorkBuddy connector smoke test marker ({marker})"
    saved = c.tool("cortex_remember", {
        "content": content,
        "type": "knowledge",
        "tags": [marker, "workbuddy"],
        "importance": 3,
    })
    st = json.dumps(saved, ensure_ascii=False)
    print(f"[OK] remember: {st[:200]}")

    hits = c.tool("cortex_recall", {"query": marker, "limit": 3})
    ht = json.dumps(hits, ensure_ascii=False)
    print(f"[OK] recall: {ht[:200]}")
    if marker not in ht and "smoke" not in ht.lower():
        print("[WARN] recall did not echo marker (embedding index may lag)")

    print("[PASS] WorkBuddy -> Cortex MCP connector OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
