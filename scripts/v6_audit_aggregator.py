"""V6.0.1 audit log aggregator (P0 Step 2).

读 memory_access_audit 表, 聚合:
- query 类型分布 (top queries)
- latency 分布 (p50/p95/p99)
- result_ids 命中频次 (top chunks)
- 按 hour/day 的调用模式
- actor 来源分布

输出: 一个 JSON report + stdout summary.

用法:
    python scripts/v6_audit_aggregator.py --hours 24
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# 复用 setup_dev_schema 的 PG 凭据 (cortex 在 8.2)
import psycopg2
import psycopg2.extras

PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "postgres"
PG_PASS = "change_me"
PG_DB = "postgres"


def main():
    parser = argparse.ArgumentParser(description="V6 audit aggregator")
    parser.add_argument("--hours", type=int, default=24, help="lookback window (hours)")
    parser.add_argument("--output", type=str, default="/tmp/v6_audit_report.json", help="output JSON path")
    args = parser.parse_args()

    print("=" * 70)
    print(f"V6 Audit Aggregator — last {args.hours}h")
    print("=" * 70)

    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. 总调用次数 (actor=search_endpoint_auto, 表示自动 audit)
    cur.execute("""
        SELECT count(*) as total
        FROM memory_access_audit
        WHERE created_at > NOW() - interval %s
          AND details->>'actor' = 'search_endpoint_auto'
    """, (f"{args.hours} hours",))
    total = cur.fetchone()["total"]
    print(f"\nTotal search audits: {total}")

    if total == 0:
        print("No data yet. Wait for /search to be called.")
        return

    # 2. 拉所有 audit 数据 (限于 hours)
    cur.execute("""
        SELECT created_at, details
        FROM memory_access_audit
        WHERE created_at > NOW() - interval %s
          AND details->>'actor' = 'search_endpoint_auto'
        ORDER BY created_at DESC
    """, (f"{args.hours} hours",))
    rows = cur.fetchall()

    # 3. Query 分布 (top queries by frequency)
    query_counter = Counter()
    for r in rows:
        q = r["details"].get("query", "")
        if q:
            query_counter[q] += 1
    print(f"\n=== TOP 10 QUERIES ({len(query_counter)} unique) ===")
    for q, n in query_counter.most_common(10):
        print(f"  {n:3}×  {q[:80]}")

    # 4. Latency 分布
    latencies = [r["details"].get("latency_ms", 0) for r in rows if r["details"].get("latency_ms") is not None]
    if latencies:
        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)
        p50 = latencies_sorted[n // 2]
        p95 = latencies_sorted[int(n * 0.95)]
        p99 = latencies_sorted[min(int(n * 0.99), n - 1)]
        print(f"\n=== LATENCY (ms) ===")
        print(f"  n={n}, min={min(latencies):.1f}, p50={p50:.1f}, p95={p95:.1f}, p99={p99:.1f}, max={max(latencies):.1f}")
        # 看超阈值比例 (用户级 SLA: 1s)
        slow_1s = sum(1 for l in latencies if l > 1000)
        slow_3s = sum(1 for l in latencies if l > 3000)
        print(f"  >1000ms: {slow_1s} ({slow_1s/n*100:.1f}%)")
        print(f"  >3000ms: {slow_3s} ({slow_3s/n*100:.1f}%)")

    # 5. Top memory_ids (被检索频次)
    chunk_counter = Counter()
    for r in rows:
        ids = r["details"].get("result_ids_top10", [])
        for cid in ids:
            chunk_counter[cid] += 1
    print(f"\n=== TOP 20 CHUNKS RETRIEVED ({len(chunk_counter)} unique) ===")
    for cid, n in chunk_counter.most_common(20):
        print(f"  {n:3}×  {cid}")

    # 6. Tags / min_importance 用法
    tags_counter = Counter()
    importance_counter = Counter()
    for r in rows:
        tags = r["details"].get("tags")
        if tags:
            for t in tags:
                tags_counter[t] += 1
        mi = r["details"].get("min_importance")
        if mi is not None:
            importance_counter[mi] += 1
    print(f"\n=== TAGS USED ===")
    for t, n in tags_counter.most_common(10):
        print(f"  {n:3}×  {t}")
    print(f"\n=== MIN_IMPORTANCE USED ===")
    for mi, n in sorted(importance_counter.items()):
        print(f"  importance={mi}: {n}×")

    # 7. 按小时分布
    hourly = defaultdict(int)
    for r in rows:
        hourly[r["created_at"].strftime("%Y-%m-%d %H:00")] += 1
    print(f"\n=== HOURLY DISTRIBUTION ===")
    for hr in sorted(hourly.keys()):
        bar = "█" * min(hourly[hr], 50)
        print(f"  {hr}  {hourly[hr]:4}×  {bar}")

    # 8. 写 JSON report
    report = {
        "window_hours": args.hours,
        "total_audits": total,
        "unique_queries": len(query_counter),
        "top_queries": query_counter.most_common(50),
        "latency": {
            "n": len(latencies),
            "min": min(latencies) if latencies else None,
            "p50": p50 if latencies else None,
            "p95": p95 if latencies else None,
            "p99": p99 if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "top_chunks": chunk_counter.most_common(50),
        "tags_used": dict(tags_counter),
        "min_importance_used": dict(importance_counter),
        "hourly_distribution": dict(hourly),
    }
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nJSON report saved: {args.output}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
