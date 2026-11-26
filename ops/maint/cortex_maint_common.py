"""
cortex_maint_common.py — shared MCP client + helpers for maintenance scripts.

Design:
- Direct HTTP/JSON-RPC to /mcp endpoint, no fastmcp SDK dependency
- Session-scoped (one TCP connection per script run)
- All calls return parsed dict; raise CortexCallError on protocol error
- Compact JSON output (one line per call) for cron log readability
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Cortex MCP endpoint
MCP_URL = os.environ.get("CORTEX_MCP_URL", "http://127.0.0.1:1880/mcp/")
PROTOCOL_VERSION = "2025-03-26"

LOG_DIR = "/opt/cortex-deploy/logs"


class CortexCallError(Exception):
    pass


def _http_post(url, payload, sid=None, timeout=30):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        raise CortexCallError(
            f"HTTP {e.code} on {payload.get('method')}: {body[:200]}"
        ) from e
    except urllib.error.URLError as e:
        raise CortexCallError(f"Connection error: {e}") from e


def _parse_sse(raw):
    """Parse the SSE 'data:' line from a streamable-http response."""
    for line in raw.split("\n"):
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise CortexCallError(f"no data: line in response: {raw[:200]}")


class CortexClient:
    def __init__(self, url=MCP_URL, client_name="cortex-maint"):
        self.url = url
        self.client_name = client_name
        self.sid = None
        self.call_count = 0
        self.error_count = 0
        self._connect()

    def _connect(self):
        resp, raw = _http_post(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": "1.0"},
            },
        })
        self.sid = resp.headers.get("Mcp-Session-Id")
        if not self.sid:
            raise CortexCallError(f"no session id from initialize; raw={raw[:200]}")
        # send initialized notification
        _http_post(self.url,
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   sid=self.sid)

    def call(self, tool_name, arguments=None, retries=1):
        """Call a tool via MCP. Returns parsed result.content[0].text as dict.

        Raises CortexCallError on protocol/JSON-RPC error or HTTP failure.
        """
        if arguments is None:
            arguments = {}
        self.call_count += 1
        last_err = None
        for attempt in range(retries + 1):
            try:
                resp, raw = _http_post(self.url, {
                    "jsonrpc": "2.0", "id": 100 + self.call_count,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }, sid=self.sid, timeout=60)
                msg = _parse_sse(raw)
                if "error" in msg:
                    err = msg["error"]
                    self.error_count += 1
                    raise CortexCallError(
                        f"{tool_name}: {err.get('code')} {err.get('message')}"
                    )
                content = msg.get("result", {}).get("content", [])
                if not content:
                    return {}
                first = content[0]
                if first.get("type") == "text":
                    txt = first.get("text", "")
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError:
                        return {"_raw_text": txt}
                return {"_content_type": first.get("type")}
            except CortexCallError as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1)
                    # try to reconnect
                    try:
                        self._connect()
                    except Exception:
                        pass
                    continue
                self.error_count += 1
                raise
        raise last_err


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def log_path(script_name):
    """Return the log file path for today."""
    ensure_log_dir()
    today = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"{script_name}_{today}.log")


def append_log(script_name, line):
    """Append a single line to today's log file (with timestamp)."""
    path = log_path(script_name)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {line}\n")


def safe_call(client, tool_name, args=None, log=True, script_name=None):
    """Wrap client.call: log success/failure, return (ok, result|errstr)."""
    try:
        result = client.call(tool_name, args or {})
        snippet = json.dumps(result, ensure_ascii=False)
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        if log and script_name:
            append_log(script_name, f"OK  {tool_name}({json.dumps(args or {}, ensure_ascii=False)}) -> {snippet}")
        return True, result
    except CortexCallError as e:
        if log and script_name:
            append_log(script_name, f"ERR {tool_name}({json.dumps(args or {}, ensure_ascii=False)}) -> {e}")
        return False, str(e)
    except Exception as e:
        if log and script_name:
            append_log(script_name, f"ERR {tool_name}({json.dumps(args or {}, ensure_ascii=False)}) -> {type(e).__name__}: {e}")
        return False, f"{type(e).__name__}: {e}"


def main_entry(name):
    """Common bootstrap: setup client, return (client, script_name)."""
    script_name = name
    ensure_log_dir()
    return CortexClient(client_name=script_name), script_name


if __name__ == "__main__":
    # smoke test
    cli = CortexClient(client_name="maint-smoke")
    ok, res = safe_call(cli, "cortex_health", script_name="smoke")
    print("health:", ok, res)
    print(f"calls={cli.call_count} errors={cli.error_count}")
