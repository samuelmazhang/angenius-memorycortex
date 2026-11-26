"""LOCOMO recall evaluation against cortex v6.5.0.

Reads ``data/locomo/data/locomo10.json``, runs every QA's question
through the v6.5.0 /search endpoint (Strategy A: Nomic + FTS5 hybrid)
and a direct PG tsv @@ query (Strategy C: FTS5 only), then computes
recall@K and MRR per strategy.

Ground truth: each QA has a list of evidence dialog ids
(``qa.evidence == ["D1:3", ...]``). For each retrieved chunk we look at
its ``metadata.dia_id``. A chunk is "correct" iff its dia_id is in
the QA's evidence set.

Two strategies (A and C). The "B (MiniLM zero-pad)" path is omitted
because the corpus is now 100% Nomic-embedded and a zero-padded
MiniLM query vector would not be a fair comparator (see
``_ab_report.py`` docstring for full reasoning).

Usage:
    python _locomo_recall.py --limit-queries 100 \\
        --nomic-url http://127.0.0.1:1880 \\
        --pg-dsn "host=127.0.0.1 port=5433 user=postgres password=change_me dbname=postgres" \\
        --top-k 5 --out results.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Strategy A: /search (Nomic + FTS5 hybrid)
# ---------------------------------------------------------------------------
def _search_hits(
    base_url: str, query: str, top_k: int, *, include_metadata: bool = True
) -> List[Tuple[str, Optional[Set[str]]]]:
    """Return list of (chunk_id, evidence_set) where evidence_set is the
    set of dia_ids in the chunk's metadata (None if metadata was missing).
    """
    body = {
        "query": query,
        "limit": top_k,
        "hybrid": True,
        "min_importance": 0,
    }
    if include_metadata:
        body["include_metadata"] = True
    req = urllib.request.Request(
        f"{base_url}/search",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = json.loads(r.read())
    out = []
    for hit in payload.get("results", []):
        cid = hit.get("id") or hit.get("chunk_id")
        meta = hit.get("metadata") or {}
        # dia_id may be a string (single) or list (rare). Normalize.
        dia_ids: Set[str] = set()
        v = meta.get("dia_id")
        if isinstance(v, str):
            dia_ids.add(v)
        elif isinstance(v, list):
            dia_ids.update(x for x in v if isinstance(x, str))
        out.append((cid, dia_ids if dia_ids else None))
    return out


# ---------------------------------------------------------------------------
# Strategy C: direct PG tsv @@ query
# ---------------------------------------------------------------------------
def _fts5_hits(
    query: str,
    top_k: int,
    pg_dsn: str,
) -> List[Tuple[str, Optional[Set[str]]]]:
    import psycopg2
    conn = psycopg2.connect(pg_dsn)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, metadata
        FROM memory_chunks
        WHERE archived = 0
          AND tsv @@ websearch_to_tsquery('simple', %s)
          AND metadata ? 'dia_id'
        ORDER BY ts_rank(tsv, websearch_to_tsquery('simple', %s)) DESC
        LIMIT %s
        """,
        (query, query, top_k),
    )
    out = []
    for cid, meta_raw in cur.fetchall():
        dia_ids: Set[str] = set()
        if isinstance(meta_raw, dict):
            v = meta_raw.get("dia_id")
            if isinstance(v, str):
                dia_ids.add(v)
            elif isinstance(v, list):
                dia_ids.update(x for x in v if isinstance(x, str))
        out.append((cid, dia_ids if dia_ids else None))
    cur.close()
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def recall_at_k(
    hits: List[Tuple[str, Optional[Set[str]]]],
    evidence: List[str],
    k: int,
) -> bool:
    """True iff ANY top-k hit's dia_id is in the QA's evidence set."""
    if not evidence:
        return False
    ev = set(evidence)
    for _, dia_ids in hits[:k]:
        if dia_ids and (dia_ids & ev):
            return True
    return False


def mrr(
    hits: List[Tuple[str, Optional[Set[str]]]],
    evidence: List[str],
) -> float:
    if not evidence:
        return 0.0
    ev = set(evidence)
    for rank, (_, dia_ids) in enumerate(hits, start=1):
        if dia_ids and (dia_ids & ev):
            return 1.0 / rank
    return 0.0


def precision_at_k(
    hits: List[Tuple[str, Optional[Set[str]]]],
    evidence: List[str],
    k: int,
) -> float:
    """Fraction of top-K hits whose dia_id is in the evidence set.
    Measures "no irrelevant chunks" -- the design-goal property
    that v6.5.0 retrieval should not mix in unrelated content.
    """
    if not hits[:k]:
        return 0.0
    if not evidence:
        return 0.0
    ev = set(evidence)
    relevant = 0
    for _, dia_ids in hits[:k]:
        if dia_ids and (dia_ids & ev):
            relevant += 1
    return relevant / k


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--data",
        default=r".data\locomo\data\locomo10.json",
    )
    p.add_argument("--nomic-url", default="http://127.0.0.1:1880")
    p.add_argument(
        "--pg-dsn",
        default="host=127.0.0.1 port=5433 user=postgres password=change_me dbname=postgres",
    )
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--limit-queries", type=int, default=None)
    p.add_argument("--out", default="locomo_recall_results.json")
    args = p.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    queries = []
    for s in data:
        for q in s.get("qa", []):
            queries.append({
                "sample_id": s["sample_id"],
                "qa_id": f"{s['sample_id']}::{q.get('question', '')[:50]}",
                "question": q["question"],
                "answer": q.get("answer", ""),
                "category": q.get("category"),
                "evidence": q.get("evidence", []),
            })
    if args.limit_queries:
        queries = queries[:args.limit_queries]
    print(f"Loaded {len(queries)} LOCOMO queries", file=sys.stderr)

    # Run
    top_ks = (1, 3, 5, 10)
    results = {
        "n_queries": len(queries),
        "top_k": args.top_k,
        "nomic_url": args.nomic_url,
        "queries": [],
    }
    A_recall = {k: 0 for k in top_ks}
    A_precision_sum = {k: 0.0 for k in top_ks}
    A_mrr_sum = 0.0
    C_recall = {k: 0 for k in top_ks}
    C_precision_sum = {k: 0.0 for k in top_ks}
    C_mrr_sum = 0.0
    A_lat = []
    C_lat = []

    cat_keys = ("1", "2", "3", "4", "5")
    per_cat = {c: {"n": 0, "A_mrr": 0.0, "C_mrr": 0.0,
                    "A_r5": 0, "C_r5": 0,
                    "A_p5_sum": 0.0, "C_p5_sum": 0.0}
              for c in cat_keys}

    for i, q in enumerate(queries):
        ev = q.get("evidence", [])
        cat = str(q.get("category", ""))
        cat_metrics = per_cat.setdefault(
            cat, {"n": 0, "A_mrr": 0.0, "C_mrr": 0.0,
                  "A_r5": 0, "C_r5": 0,
                  "A_p5_sum": 0.0, "C_p5_sum": 0.0}
        )
        cat_metrics["n"] += 1

        # A
        t0 = time.perf_counter()
        try:
            a_hits = _search_hits(args.nomic_url, q["question"], args.top_k)
            a_err = None
        except Exception as e:
            a_hits, a_err = [], str(e)
        A_lat.append((time.perf_counter() - t0) * 1000)

        a_mrr = mrr(a_hits, ev)
        A_mrr_sum += a_mrr
        for k in top_ks:
            if recall_at_k(a_hits, ev, k):
                A_recall[k] += 1
            a_p = precision_at_k(a_hits, ev, k)
            A_precision_sum[k] += a_p
        cat_metrics["A_mrr"] += a_mrr
        if recall_at_k(a_hits, ev, 5):
            cat_metrics["A_r5"] += 1
        cat_metrics["A_p5_sum"] += precision_at_k(a_hits, ev, 5)

        # C
        t0 = time.perf_counter()
        try:
            c_hits = _fts5_hits(q["question"], args.top_k, args.pg_dsn)
            c_err = None
        except Exception as e:
            c_hits, c_err = [], str(e)
        C_lat.append((time.perf_counter() - t0) * 1000)

        c_mrr = mrr(c_hits, ev)
        C_mrr_sum += c_mrr
        for k in top_ks:
            if recall_at_k(c_hits, ev, k):
                C_recall[k] += 1
            c_p = precision_at_k(c_hits, ev, k)
            C_precision_sum[k] += c_p
        cat_metrics["C_mrr"] += c_mrr
        if recall_at_k(c_hits, ev, 5):
            cat_metrics["C_r5"] += 1
        cat_metrics["C_p5_sum"] += precision_at_k(c_hits, ev, 5)

        if (i + 1) % 200 == 0 or i + 1 == len(queries):
            print(f"  {i+1}/{len(queries)}: A_mrr={A_mrr_sum/(i+1):.3f} "
                  f"C_mrr={C_mrr_sum/(i+1):.3f}", file=sys.stderr)

    n = len(queries)
    summary = {
        "n_queries": n,
        "A": {
            "recall_at_1": A_recall[1] / n,
            "recall_at_3": A_recall[3] / n,
            "recall_at_5": A_recall[5] / n,
            "recall_at_10": A_recall[10] / n,
            "precision_at_1": A_precision_sum[1] / n,
            "precision_at_3": A_precision_sum[3] / n,
            "precision_at_5": A_precision_sum[5] / n,
            "precision_at_10": A_precision_sum[10] / n,
            "mrr": A_mrr_sum / n,
            "latency_p50_ms": statistics.median(A_lat) if A_lat else 0.0,
            "latency_p95_ms": (statistics.quantiles(A_lat, n=20)[18]
                                if len(A_lat) >= 20
                                else max(A_lat, default=0.0)),
        },
        "C": {
            "recall_at_1": C_recall[1] / n,
            "recall_at_3": C_recall[3] / n,
            "recall_at_5": C_recall[5] / n,
            "recall_at_10": C_recall[10] / n,
            "precision_at_1": C_precision_sum[1] / n,
            "precision_at_3": C_precision_sum[3] / n,
            "precision_at_5": C_precision_sum[5] / n,
            "precision_at_10": C_precision_sum[10] / n,
            "mrr": C_mrr_sum / n,
            "latency_p50_ms": statistics.median(C_lat) if C_lat else 0.0,
            "latency_p95_ms": (statistics.quantiles(C_lat, n=20)[18]
                                if len(C_lat) >= 20
                                else max(C_lat, default=0.0)),
        },
        "per_category": {
            c: {
                "n": m["n"],
                "A_mrr": m["A_mrr"] / m["n"] if m["n"] else 0.0,
                "C_mrr": m["C_mrr"] / m["n"] if m["n"] else 0.0,
                "A_recall_at_5": m["A_r5"] / m["n"] if m["n"] else 0.0,
                "C_recall_at_5": m["C_r5"] / m["n"] if m["n"] else 0.0,
                "A_precision_at_5": m["A_p5_sum"] / m["n"] if m["n"] else 0.0,
                "C_precision_at_5": m["C_p5_sum"] / m["n"] if m["n"] else 0.0,
            }
            for c, m in per_cat.items()
        },
    }
    out = {"summary": summary, "n_queries": n}
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}", file=sys.stderr)
    print(json.dumps(summary["A"], indent=2), file=sys.stderr)
    print(json.dumps(summary["C"], indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())