#!/usr/bin/env python3
"""One-shot migration: parse MEMORY.md + USER.md and call cortex_remember
for each entry. Idempotent — entries tagged with source='hermes-md-migration-2026-08-23'
so we can spot duplicates via /stats/by-source if re-run.

Outputs:
  - stdout: JSON summary {ok_count, fail_count, chunks: [...]}
  - file: migration-result.json with full per-entry results
"""
import json
import sys
import os
import urllib.request
import urllib.error
from pathlib import Path

CORTEX_URL = os.environ.get("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
USER_ID = "tianxingjian-shared"  # matches existing cortex records
SOURCE = "hermes-md-migration-2026-08-23"
ENTRY_DELIMITER = "\n§\n"

def parse_md(path):
    text = Path(path).read_text(encoding="utf-8")
    return [x.strip() for x in text.split(chr(167)) if x.strip()]

def cortex_remember(content, target, importance, tags):
    """POST /chunks directly (mirrors mcp__cortex__cortex_remember shape)."""
    payload = {
        "content": content,
        "type": "general",
        "tags": tags,
        "source": SOURCE,
        "importance": importance,
        "metadata": {
            "user_id": USER_ID,
            "memory_target": target,
            "migration_origin": "hermes-md-2026-08-23",
            "char_count": len(content),
        },
    }
    req = urllib.request.Request(
        f"{CORTEX_URL}/chunks",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "chunk_id": data.get("chunk_id") or data.get("id"), "raw": data}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

# Parse both files
mem_dir = Path(r"A:/Application/Hermes-Agent/UserData/memories")
entries = []
for name, target in [("MEMORY", "memory"), ("USER", "user")]:
    parts = parse_md(mem_dir / f"{name}.md")
    for idx, content in enumerate(parts, 1):
        entries.append({
            "source_file": f"{name}.md",
            "target": target,
            "idx": idx,
            "chars": len(content),
            "content": content,
        })

print(f"Parsed: {len(entries)} entries (MEMORY + USER)", file=sys.stderr)

# Importance mapping: target=memory → 7 (long), target=user → 9 (long, near-permanent)
def importance_for(target):
    return 9 if target == "user" else 7

# Tags — useful for /stats/by-source filtering later
def tags_for(e):
    base = ["hermes-migration", "2026-08-23", e["target"]]
    # Add a content-based heuristic tag
    c = e["content"].lower()
    if "mcp-graph" in c or "mcp_graph" in c:
        base.append("mcp-graph")
    if "playwright" in c:
        base.append("playwright")
    if "cortex" in c or "memory-cortex" in c:
        base.append("cortex")
    if "vs code" in c or "vscode" in c:
        base.append("vscode")
    if "windows" in c or "msys" in c or "powershell" in c:
        base.append("windows")
    if "user profile" in c or "user preference" in c or "preference" in c:
        base.append("user-pref")
    return base

results = []
ok = 0
fail = 0
for e in entries:
    r = cortex_remember(
        content=e["content"],
        target=e["target"],
        importance=importance_for(e["target"]),
        tags=tags_for(e),
    )
    rec = {**e, "result": r}
    results.append(rec)
    if r["ok"]:
        ok += 1
        print(f"  OK [{e['source_file']}#{e['idx']}] {e['chars']} chars -> {r['chunk_id']}", file=sys.stderr)
    else:
        fail += 1
        print(f"  FAIL [{e['source_file']}#{e['idx']}] {e['chars']} chars: {r['error']}", file=sys.stderr)

# Write result file
out_path = Path(r".migration-result.json")
out_path.write_text(
    json.dumps({"ok": ok, "fail": fail, "results": results}, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(json.dumps({"ok": ok, "fail": fail, "result_file": str(out_path)}))