"""LOCOMO-1986 retrieval eval against Cortex 6.7.0 (cross-system comparison).

Same metric definitions and ground-truth rule as ``_locomo_recall.py``
(dia_id in QA.evidence), but uses only Strategy A (the live /search
endpoint) because FTS5-only baseline is version-independent and already
frozen in ``_locomo_full_sample.json``.

This run produces the Cortex side of the cross-system comparison table.

Usage:
    python _locomo_recall_6_7.py --out locomo_6_7_results.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

DEFAULT_DATA = r".data\locomo\data\locomo10.json"
DEFAULT_URL = "http://127.0.0.1:1880"


def search_hits(base_url: str, query: str, top_k: int) -> List[Tuple[str, Set[str]]]:
    body = {
        "query": query,
        "limit": top_k,
        "hybrid": True,
        "min_importance": 0,
        "include_metadata": True,
    }
    req = urllib.request.Request(
        f"{base_url}/search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    out: List[Tuple[str, Set[str]]] = []
    for hit in payload.get("results", []):
        cid = hit.get("id") or hit.get("chunk_id") or ""
        meta = hit.get("metadata") or {}
        dia_ids: Set[str] = set()
        v = meta.get("dia_id")
        if isinstance(v, str):
            dia_ids.add(v)
        elif isinstance(v, list):
            dia_ids.update(x for x in v if isinstance(x, str))
        out.append((cid, dia_ids))
    return out


def recall_at_k(hits, evidence, k):
    if not evidence:
        return False
    ev = set(evidence)
    return any(dia and (dia & ev) for _, dia in hits[:k])


def mrr(hits, evidence):
    if not evidence:
        return 0.0
    ev = set(evidence)
    for rank, (_, dia) in enumerate(hits, 1):
        if dia and (dia & ev):
            return 1.0 / rank
    return 0.0


def precision_at_k(hits, evidence, k):
    if not evidence or not hits[:k]:
        return 0.0
    ev = set(evidence)
    return sum(1 for _, dia in hits[:k] if dia and (dia & ev)) / k


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--base-url", default=DEFAULT_URL)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--out", default="locomo_6_7_results.json")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    queries = []
    for s in data:
        for q in s.get("qa", []):
            queries.append({
                "question": q["question"],
                "evidence": q.get("evidence", []),
                "category": str(q.get("category", "")),
            })
    print(f"Loaded {len(queries)} LOCOMO queries", file=sys.stderr)

    top_ks = (1, 3, 5, 10)
    recall = {k: 0 for k in top_ks}
    prec_sum = {k: 0.0 for k in top_ks}
    mrr_sum = 0.0
    lats: List[float] = []
    per_cat: Dict[str, Dict[str, Any]] = {}
    errors = 0

    for i, q in enumerate(queries):
        ev = q["evidence"]
        cat = q["category"]
        c = per_cat.setdefault(cat, {"n": 0, "mrr": 0.0, "r5": 0, "p5": 0.0})
        c["n"] += 1
        t0 = time.perf_counter()
        try:
            hits = search_hits(args.base_url, q["question"], 10)
        except Exception as exc:  # keep the sweep alive
            errors += 1
            hits = []
            if errors <= 5:
                print(f"  query {i+1} error: {exc}", file=sys.stderr)
        lats.append((time.perf_counter() - t0) * 1000.0)

        m = mrr(hits, ev)
        mrr_sum += m
        c["mrr"] += m
        for k in top_ks:
            if recall_at_k(hits, ev, k):
                recall[k] += 1
            prec_sum[k] += precision_at_k(hits, ev, k)
        if recall_at_k(hits, ev, 5):
            c["r5"] += 1
        c["p5"] += precision_at_k(hits, ev, 5)

        if (i + 1) % 200 == 0 or i + 1 == len(queries):
            print(f"  {i+1}/{len(queries)}: MRR={mrr_sum/(i+1):.4f} R@5={recall[5]/(i+1):.4f}", file=sys.stderr)

    n = len(queries)
    summary = {
        "version": "6.7.0",
        "n_queries": n,
        "errors": errors,
        "recall_at_1": recall[1] / n,
        "recall_at_3": recall[3] / n,
        "recall_at_5": recall[5] / n,
        "recall_at_10": recall[10] / n,
        "precision_at_1": prec_sum[1] / n,
        "precision_at_3": prec_sum[3] / n,
        "precision_at_5": prec_sum[5] / n,
        "precision_at_10": prec_sum[10] / n,
        "mrr": mrr_sum / n,
        "latency_p50_ms": statistics.median(lats),
        "latency_p95_ms": statistics.quantiles(lats, n=20)[18] if len(lats) >= 20 else max(lats),
        "per_category": {
            cat: {
                "n": c["n"],
                "recall_at_5": c["r5"] / c["n"],
                "precision_at_5": c["p5"] / c["n"],
                "mrr": c["mrr"] / c["n"],
            }
            for cat, c in sorted(per_cat.items())
        },
    }
    out = {"summary": summary, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.out}", file=sys.stderr)
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
