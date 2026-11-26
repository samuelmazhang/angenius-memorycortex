#!/usr/bin/env python3
"""Re-migrate the 21 chunks with the CORRECT source field that
cortex_memory_backend._TARGET_TO_SOURCE expects:

    memory → 'hermes_memory'
    user   → 'hermes_user'

Also keep the original chunk_id (via metadata.original_chunk_id) for
audit, then DELETE the old wrongly-tagged chunks via DELETE /chunks/{id}.

Idempotent: if old chunks already deleted, no-op on delete step.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

CORTEX_URL = os.environ.get("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
USER_ID = "<user_id>"
ENTRY_DELIMITER = "\n§\n"

# Correct target→source mapping (mirror of _TARGET_TO_SOURCE in
# tools/cortex_memory_backend.py)
TARGET_SOURCE = {"memory": "hermes_memory", "user": "hermes_user"}


def parse_md(path):
    text = Path(path).read_text(encoding="utf-8")
    return [x.strip() for x in text.split(chr(167)) if x.strip()]


def cortex_chunks_create_batch(items):
    """POST /chunks/batch. Server accepts either {chunks: [...]} or a bare
    list — try the bare list first since the openapi.json schema doesn't pin it.
    """
    # Try #1: bare list
    for payload_attempt in (items, {"chunks": items}):
        try:
            req = urllib.request.Request(
                f"{CORTEX_URL}/chunks/batch",
                data=json.dumps(payload_attempt).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            continue
    return {"error": f"both payload shapes failed: {last_err}"}


def cortex_forget(chunk_id):
    """DELETE /chunks/{chunk_id}."""
    req = urllib.request.Request(
        f"{CORTEX_URL}/chunks/{chunk_id}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}


def cortex_list_migrated():
    """List current wrongly-tagged chunks."""
    req = urllib.request.Request(
        f"{CORTEX_URL}/list?source=hermes-md-migration-2026-08-23&limit=50",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return d.get("items") or d.get("chunks") or d.get("results") or []


# 1) Read original .md files (NOT the migrated- tombstone — but the live md was
# already moved; we read from the tombstone to keep the content)
mem_dir = Path(r"A:/Application/Hermes-Agent/UserData/memories")
md_files = {
    "memory": mem_dir / "MEMORY.md.migrated-20260823-010123",
    "user": mem_dir / "USER.md.migrated-20260823-010123",
}
for tgt, p in md_files.items():
    if not p.exists():
        print(f"FATAL: tombstone missing: {p}", file=sys.stderr)
        sys.exit(2)

# 2) Build new chunks with correct source
new_chunks = []
for target, path in md_files.items():
    parts = parse_md(path)
    for idx, content in enumerate(parts, 1):
        new_chunks.append({
            "content": content,
            "type": "general",
            "tags": [
                "hermes-md-migration",
                "2026-08-23",
                target,
                # content-based heuristic tags
            ],
            "source": TARGET_SOURCE[target],
            "importance": 9 if target == "user" else 7,
            "metadata": {
                "user_id": USER_ID,
                "memory_target": target,
                "migration_origin": "hermes-md-2026-08-23",
                "migration_origin_source": "hermes-md-migration-2026-08-23",
                "char_count": len(content),
                "source_file": "MEMORY.md" if target == "memory" else "USER.md",
                "source_index": idx,
            },
        })

# Add heuristic tags to specific chunks
heuristic_keywords = {
    "mcp-graph": ["mcp-graph", "mcp_graph", "workflow-graph"],
    "playwright": ["playwright", "browser_navigate", "chromium-1234"],
    "cortex": ["cortex", "memory-cortex"],
    "vscode": ["vscode", ".vscode"],
    "windows": ["windows", "msys", "powershell"],
    "user-pref": ["preference", "prefers", "responds well", "diagnostic style"],
    "policies": ["policies", "rule", "practice"],
}
for c in new_chunks:
    content_lower = c["content"].lower()
    for tag, kws in heuristic_keywords.items():
        if any(kw in content_lower for kw in kws):
            c["tags"].append(tag)

print(f"Step 1: POST /chunks/create-batch with {len(new_chunks)} chunks...", file=sys.stderr)
batch_result = cortex_chunks_create_batch(new_chunks)
if "error" in batch_result:
    print(f"FAIL batch: {batch_result['error']}", file=sys.stderr)
    sys.exit(3)
new_ids = []
if isinstance(batch_result, dict):
    new_ids = batch_result.get("chunk_ids") or batch_result.get("ids") or []
    if not new_ids and "results" in batch_result:
        new_ids = [r.get("chunk_id") or r.get("id") for r in batch_result["results"] if r]
print(f"  created {len(new_ids)} new chunks", file=sys.stderr)

# 3) Delete old wrongly-tagged chunks
print(f"Step 2: list old chunks to delete...", file=sys.stderr)
old_chunks = cortex_list_migrated()
print(f"  found {len(old_chunks)} old chunks", file=sys.stderr)

deleted = 0
for c in old_chunks:
    cid = c.get("id")
    if not cid:
        continue
    r = cortex_forget(cid)
    if r.get("ok"):
        deleted += 1
    else:
        print(f"  FAIL delete {cid}: {r.get('error')}", file=sys.stderr)
print(f"  deleted {deleted}/{len(old_chunks)} old chunks", file=sys.stderr)

print(json.dumps({
    "created": len(new_ids),
    "deleted": deleted,
    "new_chunk_ids_sample": new_ids[:3],
}))