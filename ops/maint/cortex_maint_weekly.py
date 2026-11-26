#!/usr/bin/env python3
"""
cortex_maint_weekly.py — weekly deeper maintenance for cortex.

Schedule: 02:00 Sunday via cron (dry mode by default).
Default mode = DRY RUN: every destructive call gets a no-op or dry_run
parameter so nothing actually changes. --mode=apply turns on the real
operations.

Calls (in order):

  DRY mode (default, safe for any cron):
    1. cortex_forgetting_stats
    2. cortex_forgetting_expired (count)
    3. cortex_ttl_archive(days=999999)        — never actually archives
    4. cortex_reconsolidate (memory_id="DRY_RUN_NOOP") — no-op
    5. cortex_merge_duplicates (DRY marker via threshold>1.0 — invalid, skip)
    6. cortex_compress_run (max_count=0)        — no-op
    7. cortex_rumination_run — DRY: just print stats, don't run
    8. cortex_conflict_scan (dry_run=true)
    9. cortex_consolidate_facts (user_id=<user_id>, just LIST)

  APPLY mode (--mode=apply, opt-in):
    -- Content-driven ONLY (per user decision 2026-08-19):
       "old" != "should forget". Forgetting should be conflict-driven
       (new info replaces old), not age-driven (X days old => archive).
       Removed: ttl_archive, forgetting_cleanup, reconsolidate (all time-driven).
    1. cortex_merge_duplicates (threshold=0.95) — content-similarity dedup
    2. cortex_compress_run (max_count=50) — long-content compression
    3. cortex_conflict_scan (dry_run=False) — supersede by content conflict
    4. cortex_consolidate_run (user_id=<user_id>, min_confidence=0.7)
    5. cortex_rumination_run (min_cluster_size=5) — LLM pattern detection

Output:
  - stdout + /opt/cortex-deploy/logs/cortex_maint_weekly_YYYYMMDD.log
  - exit: 0 always
"""

import argparse
import json
import sys
from cortex_maint_common import (
    CortexClient, CortexCallError, main_entry, safe_call,
    now_iso, append_log,
)


def run_dry(client, name):
    """All calls are read-only or no-op. Safe to run any time."""
    print(f"[{now_iso()}] weekly DRY-RUN start")
    append_log(name, "=== weekly DRY-RUN start ===")

    # 1) forgetting stats
    ok, res = safe_call(client, "cortex_forgetting_stats", script_name=name)
    print(f"  forgetting_stats: {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # 2) count expired
    ok, res = safe_call(client, "cortex_forgetting_expired",
                        {"limit": 500}, script_name=name)
    if ok and isinstance(res, dict):
        items = res.get("results", res.get("chunks", []))
        if isinstance(items, list):
            print(f"  expired: {len(items)} chunks (cap 500)")
    else:
        print(f"  expired: {res}")

    # 3) conflict scan (dry)
    ok, res = safe_call(client, "cortex_conflict_scan",
                        {"lookback_days": 7, "dry_run": True, "max_buckets": 500},
                        script_name=name)
    if ok and isinstance(res, dict):
        print(f"  conflict_scan(dry,7d): buckets={res.get('buckets_scanned','?')}, "
              f"superseded={res.get('superseded','?')}, errors={res.get('errors','?')}")
    else:
        print(f"  conflict_scan(dry): {res}")

    # 4) consolidate facts (just list)
    ok, res = safe_call(client, "cortex_consolidate_facts",
                        {"user_id": "<user_id>", "min_confidence": 0.5},
                        script_name=name)
    if ok and isinstance(res, dict):
        items = res.get("facts", res.get("results", []))
        if isinstance(items, list):
            print(f"  consolidate_facts: {len(items)} facts for <user_id>")
    else:
        print(f"  consolidate_facts: {res}")

    # 5) rumination stats
    ok, res = safe_call(client, "cortex_rumination_stats", script_name=name)
    print(f"  rumination: {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # 6) storage health
    ok, res = safe_call(client, "cortex_storage_health", script_name=name)
    print(f"  storage_health: {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # SKIP destructive in dry mode:
    skipped = [
        "cortex_merge_duplicates",       # destructive
        "cortex_compress_run",           # creates compressed records
        "cortex_rumination_run",         # may invoke LLM
        "cortex_conflict_scan(not dry)", # destructive
        "cortex_consolidate_run",        # mutates core_profile
    ]
    # Permanently removed (time-driven, see run_apply for rationale):
    removed = [
        "cortex_forgetting_cleanup",
        "cortex_ttl_archive",
        "cortex_reconsolidate",
    ]
    for s in skipped:
        append_log(name, f"SKIP (dry mode) {s}")
    for s in removed:
        append_log(name, f"REMOVED (content-only policy) {s}")
    print(f"  [DRY] skipped {len(skipped)} destructive ops")
    print(f"  [DRY] removed {len(removed)} time-driven ops (content-only policy)")
    print(f"  [DRY] review this log + run with --mode=apply to actually execute")

    print(f"[{now_iso()}] weekly DRY-RUN done: calls={client.call_count}, errors={client.error_count}")
    append_log(name, f"=== done (dry): calls={client.call_count}, errors={client.error_count} ===")


def run_apply(client, name):
    """Destructive operations. Only call with --mode=apply after review."""
    print(f"[{now_iso()}] weekly APPLY start (DESTRUCTIVE)")
    append_log(name, "=== weekly APPLY start (DESTRUCTIVE) ===")

    # Content-driven maintenance only (per user decision 2026-08-19:
    # "old" != "should forget" -- age-driven operations removed)
    #
    # Order matters: do conflict detection FIRST so supersede happens
    # before merge dedup (avoid merging two chunks where one will be superseded).

    # 1) conflict scan (apply, NOT dry) — supersede by content conflict
    ok, res = safe_call(client, "cortex_conflict_scan",
                        {"lookback_days": 7, "dry_run": False, "max_buckets": 500},
                        script_name=name)
    if ok and isinstance(res, dict):
        print(f"  conflict_scan(apply,7d): superseded={res.get('superseded','?')}, "
              f"errors={res.get('errors','?')}")
    else:
        print(f"  conflict_scan(apply): {res}")

    # 2) merge duplicates — content similarity dedup
    ok, res = safe_call(client, "cortex_merge_duplicates", {"threshold": 0.95},
                        script_name=name)
    print(f"  merge_duplicates(th=0.95): {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # 3) compress long chunks
    ok, res = safe_call(client, "cortex_compress_run", {"max_count": 50},
                        script_name=name)
    print(f"  compress_run(max=50): {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # 4) rumination (uses LLM)
    ok, res = safe_call(client, "cortex_rumination_run", {"min_cluster_size": 5},
                        script_name=name)
    print(f"  rumination_run: {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # 5) consolidate run for <user_id> — apply extracted facts to user core
    ok, res = safe_call(client, "cortex_consolidate_run",
                        {"user_id": "<user_id>", "min_confidence": 0.7},
                        script_name=name)
    print(f"  consolidate_run: {json.dumps(res, ensure_ascii=False)[:200] if ok else res}")

    # REMOVED (time-driven, per user 2026-08-19):
    # - cortex_ttl_archive (days=N): batch archive by age — wrong paradigm
    # - cortex_forgetting_cleanup (force=True): delete by expires_at — wrong paradigm
    # - cortex_reconsolidate (memory_id=None): weaken by age — wrong paradigm
    # Forgetting should be CONFLICT-DRIVEN, not time-driven.
    for removed in ["cortex_ttl_archive", "cortex_forgetting_cleanup", "cortex_reconsolidate"]:
        append_log(name, f"SKIP (content-only policy) {removed}")

    print(f"[{now_iso()}] weekly APPLY done: calls={client.call_count}, errors={client.error_count}")
    append_log(name, f"=== done (apply): calls={client.call_count}, errors={client.error_count} ===")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dry", "apply"], default="dry",
                        help="dry (default, safe) | apply (destructive)")
    args = parser.parse_args()

    client, name = main_entry("cortex_maint_weekly")

    if args.mode == "dry":
        run_dry(client, name)
    else:
        run_apply(client, name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
