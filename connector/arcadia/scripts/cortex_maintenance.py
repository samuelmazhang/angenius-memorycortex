#!/usr/bin/env python3
"""
cortex_maintenance.py - 后台维护脚本

按合理频率调用 cortex V5 维护类 endpoint。
被 arcadia cron 调用。

Usage:
    python cortex_maintenance.py daily      # daily 维护
    python cortex_maintenance.py weekly     # weekly 深度维护
    python cortex_maintenance.py hourly     # hourly 轻量统计
    python cortex_maintenance.py all        # 全跑（首次/调试用）

Env:
    MEMORY_CORTEX_URL  default http://127.0.0.1:1880
    MEMORY_CORTEX_TIMEOUT default 30
"""
import sys
import os
import time
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

CORTEX_URL = os.getenv("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
TIMEOUT = int(os.getenv("MEMORY_CORTEX_TIMEOUT", "30"))


def call(method: str, path: str, body=None, label: str = ""):
    """Call cortex endpoint, return (status, body, elapsed_ms). Never raises."""
    url = f"{CORTEX_URL}{path}"
    t0 = time.monotonic()
    try:
        if method in ("POST", "PUT", "PATCH"):
            data = json.dumps(body or {}).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"Content-Type": "application/json"},
            )
        else:
            req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            elapsed = int((time.monotonic() - t0) * 1000)
            body_text = r.read().decode("utf-8", errors="replace")
            print(f"  [{elapsed:>5}ms] {method:>5} {path:<45} {label or ''}", flush=True)
            return (r.status, body_text, elapsed)
    except urllib.error.HTTPError as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"  [{elapsed:>5}ms] {method:>5} {path:<45} HTTP {e.code} {label or ''}", flush=True)
        return (e.code, "", elapsed)
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"  [{elapsed:>5}ms] {method:>5} {path:<45} ERR: {type(e).__name__}: {e} {label or ''}", flush=True)
        return (-1, "", elapsed)


def _bulk_ttl_archive():
    """POST /ttl/archive requires chunk_id as a query param (single-chunk at a time).
    Server has no bulk variant, so iterate over expired chunks we just inspected."""
    # The cortex OpenAPI spec defines POST /ttl/archive with a single chunk_id
    # query param and no request body. To archive the whole expired batch we
    # page through GET /forgetting/expired (limit=100) and archive each id.
    seen_ids = set()
    next_path = "/forgetting/expired?limit=100"
    archived = 0
    while next_path:
        status, body, _ms = call("GET", next_path, label="expired page")
        if status != 200 or not body:
            break
        try:
            data = json.loads(body)
        except Exception:
            break
        ids = []
        for item in data.get("expired", []):
            cid = item.get("id") or item.get("chunk_id")
            if cid and cid not in seen_ids:
                ids.append(cid)
                seen_ids.add(cid)
        if not ids:
            break
        for cid in ids:
            call("POST", f"/ttl/archive?chunk_id={urllib.parse.quote(cid)}",
                 label=f"ttl archive {cid[:20]}")
            archived += 1
        if len(ids) < 100:
            break
        # otherwise loop and try next page (server uses limit/offset)
        # but we already consumed the only chunk_id we know about; stop to avoid
        # infinite loop in case the server doesn't paginate by offset
        break
    return archived


def daily():
    """Daily maintenance: cleanup, dedup, lightweight consolidation."""
    print(f"\n=== DAILY MAINTENANCE @ {datetime.utcnow().isoformat()} ===")
    print(f"Cortex: {CORTEX_URL}\n")
    # 1. Expiring/expired stats — decide cleanup threshold
    call("GET", "/forgetting/stats", label="forgetting stats")
    call("GET", "/forgetting/expiring-soon?days=7&limit=50", label="expiring 7d")
    call("GET", "/forgetting/expired", label="expired")
    call("GET", "/ttl/archived", label="ttl archived")
    # 2. Cleanup expired chunks (core daily job — known 500 bug, ignore failure)
    call("POST", "/forgetting/cleanup", body={"dry_run": False}, label="cleanup expired")
    # 3. Bulk archive expired TTL chunks (page through expired list)
    _bulk_ttl_archive()
    # 4. Merge duplicates (>= 0.85 similarity threshold — known 500 bug, ignore)
    call("POST", "/merge-duplicates", body={"similarity_threshold": 0.85}, label="merge duplicates")
    # 5. Lightweight compress (older than 14 days, low importance)
    call("POST", "/compress/run", body={"min_age_days": 14, "max_importance": 4, "max_per_batch": 50},
         label="compress old")
    # 6. Lightweight consolidate (extract facts)
    call("POST", "/consolidate/run", body={"memory_ids": []}, label="consolidate")
    # 7. Refresh memory importance
    call("POST", "/reconsolidate", body={}, label="reconsolidate")
    # 8. Health
    call("GET", "/storage/health", label="storage health")
    call("GET", "/health", label="health")
    print("\n=== DAILY DONE ===\n")


def weekly():
    """Weekly deep maintenance: rumination, deep heuristics, bitemporal refresh."""
    print(f"\n=== WEEKLY DEEP MAINTENANCE @ {datetime.utcnow().isoformat()} ===")
    print(f"Cortex: {CORTEX_URL}\n")
    # 1. Full storage audit
    call("GET", "/storage/health", label="storage health")
    # 2. Heavy dedup pass (lower threshold)
    call("POST", "/merge-duplicates", body={"similarity_threshold": 0.75}, label="merge duplicates (deep)")
    # 3. Heavy compress (60 days)
    call("POST", "/compress/run", body={"min_age_days": 60, "max_importance": 5, "max_per_batch": 200},
         label="compress deep")
    # 4. Apply facts to core profile (per-user consolidation)
    for uid in ["default", "<user>"]:
        call("POST", f"/core/{uid}/apply-facts", body={"min_confidence": 0.5}, label=f"apply facts {uid}")
    # 5. Rumination (reflection + clustering)
    call("POST", "/rumination/run", body={"min_cluster_size": 3}, label="rumination")
    call("GET", "/rumination/stats", label="rumination stats")
    # 6. Abstract heuristics (high-level insights)
    call("POST", "/rumination/abstract-heuristics", body={"force": False}, label="abstract heuristics")
    # 7. Bitemporal relations refresh (any pending temporal links)
    call("POST", "/relations/temporal", body={}, label="relations/temporal refresh")
    # 7b. Graph heuristic backfill (V5.3): keep the knowledge graph fresh so
    #     GraphRAG k-hop / include_related keep working as chunks accumulate.
    #     Idempotent (ON CONFLICT accumulate), safe to re-run weekly.
    call("POST", "/graph/mine",
         body={"similarity_threshold": 0.72, "similar_batch": 300, "per_anchor": 3},
         label="graph/mine backfill")
    # 8. Run multi-hop bench (detect issues)
    call("GET", "/bench/multi-hop", label="bench multi-hop")
    # 9. Eval feedback reset
    call("POST", "/eval/feedback", body={}, label="eval feedback")
    # 10. Reindex (only if needed)
    call("POST", "/reindex", body={}, label="reindex")
    print("\n=== WEEKLY DONE ===\n")


def hourly():
    """Hourly lightweight stats and storage health check."""
    print(f"\n=== HOURLY STATS @ {datetime.utcnow().isoformat()} ===")
    # Stats
    call("GET", "/stats", label="stats")
    call("GET", "/metrics", label="metrics")
    call("GET", "/ops/counters", label="ops counters")
    # Health
    call("GET", "/health", label="health")
    call("GET", "/storage/health", label="storage health")
    # Working memory snapshot
    call("GET", "/working/global", label="working global")
    # Taxonomy
    call("GET", "/taxonomy/relations", label="taxonomy relations")
    print("\n=== HOURLY DONE ===\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: cortex_maintenance.py {daily|weekly|hourly|all}")
        sys.exit(1)
    mode = sys.argv[1].lower()
    if mode == "daily":
        daily()
    elif mode == "weekly":
        weekly()
    elif mode == "hourly":
        hourly()
    elif mode == "all":
        daily()
        weekly()
        hourly()
    else:
        print(f"Unknown mode: {mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()