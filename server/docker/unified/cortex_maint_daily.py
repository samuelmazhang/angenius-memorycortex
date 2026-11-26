#!/usr/bin/env python3
"""
cortex_maint_daily.py — daily maintenance for cortex (content-driven).

Schedule: 03:00 daily via cron.

Per user decision 2026-08-19 23:30:
"把记忆冲突和记忆更新等相关处理要做到高频,防止旧记忆错误影响新的决策,导致反复"
(Conflict resolution and memory updates should be high-frequency, to prevent
old/bad memories from polluting new decisions and causing repeated mistakes.)

Strategy: daily runs BOTH read-only health checks AND content-driven
conflict resolution on TODAY's writes (lookback_days=1, narrow max_buckets).
This way, every day the new memories get reviewed and superseded if
conflicting with the same source_key bucket. Old memories (lookback_days=1
excludes them) are NEVER touched by time -- only by content conflict.

Side effects (all content-driven, per ADR-075):
  - conflict_scan(apply, lookback=1): supersedes same-day conflicts
  - consolidate_run: applies today's extracted facts to user core
  - 6 read-only calls for health monitoring

Output:
  - stdout: compact one-line per call summary
  - log: /opt/cortex-deploy/logs/cortex_maint_daily_YYYYMMDD.log
  - exit: 0 (always -- failures are logged, not raised)
"""

import json
import sys
import time
from cortex_maint_common import (
    CortexClient, CortexCallError, main_entry, safe_call,
    now_iso, append_log,
)


def run():
    client, name = main_entry("cortex_maint_daily")
    append_log(name, f"=== start daily maint ===")
    print(f"[{now_iso()}] daily maint start")

    # 1) health
    ok, res = safe_call(client, "cortex_stats_health", script_name=name)
    summary = res if ok else res
    if ok and isinstance(res, dict):
        # Real schema: db_count, vec_count, cache_size, cache_hits, cache_misses, ops
        print(f"  health: status={res.get('status', '?')}, "
              f"db_count={res.get('db_count', '?')}, "
              f"vec_count={res.get('vec_count', '?')}, "
              f"cache_size={res.get('cache_size', '?')}, "
              f"hits/misses={res.get('cache_hits', '?')}/{res.get('cache_misses', '?')}")
    else:
        print(f"  health: ERR {res}")

    # 2) forgetting stats
    ok, res = safe_call(client, "cortex_forgetting_stats", script_name=name)
    if ok and isinstance(res, dict):
        print(f"  forgetting_stats: {json.dumps(res, ensure_ascii=False)[:200]}")
    else:
        print(f"  forgetting_stats: {res}")

    # 3) expired count
    ok, res = safe_call(client, "cortex_forgetting_expired",
                        {"limit": 200}, script_name=name)
    if ok and isinstance(res, dict):
        items = res.get("results", res.get("chunks", []))
        if isinstance(items, list):
            print(f"  expired: {len(items)} chunks past due (showing up to 200)")
        else:
            print(f"  expired: {res}")
    else:
        print(f"  expired: ERR {res}")

    # 4) expiring soon
    ok, res = safe_call(client, "cortex_forgetting_expiring_soon",
                        {"days": 7, "limit": 200}, script_name=name)
    if ok and isinstance(res, dict):
        items = res.get("results", res.get("chunks", []))
        if isinstance(items, list):
            print(f"  expiring_within_7d: {len(items)} chunks")
        else:
            print(f"  expiring_soon: {res}")
    else:
        print(f"  expiring_soon: ERR {res}")

    # 5) conflict scan dry_run
    ok, res = safe_call(client, "cortex_conflict_scan",
                        {"lookback_days": 1, "dry_run": True, "max_buckets": 200},
                        script_name=name)
    if ok and isinstance(res, dict):
        print(f"  conflict_scan(dry): buckets={res.get('buckets_scanned', '?')}, "
              f"superseded={res.get('superseded', '?')}, errors={res.get('errors', '?')}")
    else:
        print(f"  conflict_scan(dry): {res}")

    # 6) rumination stats
    ok, res = safe_call(client, "cortex_rumination_stats", script_name=name)
    if ok and isinstance(res, dict):
        print(f"  rumination: {json.dumps(res, ensure_ascii=False)[:200]}")
    else:
        print(f"  rumination: ERR {res}")

    # 7) prompt cache
    ok, res = safe_call(client, "cortex_cache_pr_stats", script_name=name)
    if ok and isinstance(res, dict):
        print(f"  cache: {json.dumps(res, ensure_ascii=False)[:200]}")
    else:
        print(f"  cache: ERR {res}")

    # 8) ops counters
    ok, res = safe_call(client, "cortex_stats_ops", script_name=name)
    if ok and isinstance(res, dict):
        print(f"  ops: {json.dumps(res, ensure_ascii=False)[:200]}")
    else:
        print(f"  ops: ERR {res}")

    # === content-driven active maintenance (per ADR-075 + user 2026-08-19) ===
    # 9) conflict_scan APPLY (lookback=1, narrow max_buckets) -- prevents same-day
    #    conflicts from polluting new decisions
    ok, res = safe_call(client, "cortex_conflict_scan",
                        {"lookback_days": 1, "dry_run": False, "max_buckets": 100},
                        script_name=name)
    if ok and isinstance(res, dict):
        print(f"  conflict_scan(apply,1d): superseded={res.get('superseded','?')}, "
              f"buckets={res.get('buckets_scanned','?')}, errors={res.get('errors','?')}")
    else:
        print(f"  conflict_scan(apply): {res}")

    # 10) consolidate_run (apply today's extracted facts to user core)
    ok, res = safe_call(client, "cortex_consolidate_run",
                        {"min_confidence": 0.5}, script_name=name)
    if ok and isinstance(res, dict):
        print(f"  consolidate_run: {json.dumps(res, ensure_ascii=False)[:200]}")
    else:
        print(f"  consolidate_run: ERR {res}")

    # summary
    print(f"[{now_iso()}] daily maint done: calls={client.call_count}, errors={client.error_count}")
    append_log(name, f"=== done: calls={client.call_count}, errors={client.error_count} ===")
    return 0  # always 0; failures are logged


if __name__ == "__main__":
    sys.exit(run())
