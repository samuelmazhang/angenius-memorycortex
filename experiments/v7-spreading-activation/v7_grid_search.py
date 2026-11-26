"""V7 Spreading Activation grid search (ADR-074 PoC-2).

Grid over (decay, threshold, max_hops):
  decay ∈ {0.5, 0.7, 0.9}
  threshold ∈ {0.01, 0.05, 0.1}
  max_hops ∈ {2, 3, 4}
  = 3 × 3 × 3 = 27 组

每组跑 n=100 seed queries (importance ≥ 7 chunks from cortex_v7_dev),
算 composite (α/β/γ/δ), 输出最优组.

PASS 标准: 最优组 composite ≥ BFS composite + 0.15

用法:
    python scripts/v7_grid_search.py --n-queries 100 --seed 42
"""

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))  # experiments/v7-spreading-activation/

from spreading_activation import spread_activation, _bfs_recall
from v7_evaluate_real import (
    load_graph, load_seed_chunks,
    adj_metric, sig_metric, breadth_metric,
    PG_HOST, PG_PORT, PG_USER, PG_PASS, PG_DB, SCHEMA,
)


# Grid search space (per ADR-074 §Algorithm)
DECAY_GRID = [0.5, 0.7, 0.9]
THRESHOLD_GRID = [0.01, 0.05, 0.1]
MAX_HOPS_GRID = [2, 3, 4]


def run_one_config(seeds_data, graph, importance_map, decay, threshold, max_hops, k=10):
    """Run SA with given (decay, threshold, max_hops), aggregate metrics over all seeds."""
    sa_adj, sa_sig, sa_brd, sa_comp = [], [], [], []
    skipped = 0
    for sc in seeds_data:
        seed = sc["id"]
        if not seed or seed not in graph:
            skipped += 1
            continue
        try:
            sa_result = spread_activation([seed], graph, decay=decay, threshold=threshold, max_hops=max_hops)
            sa_ids = [cid for cid, _ in sa_result if cid != seed][:k]
            if not sa_ids:
                skipped += 1
                continue
            a = adj_metric(sa_ids, seed, graph)
            s = sig_metric(sa_ids, importance_map)
            b = breadth_metric(sa_ids)
            c = 0.4 * a + 0.3 * s + 0.3 * b
            sa_adj.append(a); sa_sig.append(s); sa_brd.append(b); sa_comp.append(c)
        except Exception:
            skipped += 1
    if not sa_comp:
        return None
    n = len(sa_comp)
    return {
        "n_evaluated": n,
        "skipped": skipped,
        "adjacency": sum(sa_adj) / n,
        "significance": sum(sa_sig) / n,
        "breadth": sum(sa_brd) / n,
        "composite": sum(sa_comp) / n,
    }


def run_bfs_baseline(seeds_data, graph, importance_map, k=10):
    """Run V6 BFS baseline for comparison."""
    bfs_adj, bfs_sig, bfs_brd, bfs_comp = [], [], [], []
    skipped = 0
    for sc in seeds_data:
        seed = sc["id"]
        if not seed or seed not in graph:
            skipped += 1
            continue
        try:
            bfs_result = _bfs_recall([seed], graph, min_strength=0.5, cap=6)
            bfs_ids = bfs_result[:k]
            if not bfs_ids:
                skipped += 1
                continue
            a = adj_metric(bfs_ids, seed, graph)
            s = sig_metric(bfs_ids, importance_map)
            b = breadth_metric(bfs_ids)
            c = 0.4 * a + 0.3 * s + 0.3 * b
            bfs_adj.append(a); bfs_sig.append(s); bfs_brd.append(b); bfs_comp.append(c)
        except Exception:
            skipped += 1
    if not bfs_comp:
        return None
    n = len(bfs_comp)
    return {
        "n_evaluated": n,
        "skipped": skipped,
        "adjacency": sum(bfs_adj) / n,
        "significance": sum(bfs_sig) / n,
        "breadth": sum(bfs_brd) / n,
        "composite": sum(bfs_comp) / n,
    }


def main():
    parser = argparse.ArgumentParser(description="V7 SA grid search")
    parser.add_argument("--n-queries", type=int, default=100, help="number of seed queries")
    parser.add_argument("--k", type=int, default=10, help="top-K")
    parser.add_argument("--seed", type=int, default=42, help="random seed for seed selection")
    parser.add_argument("--output", type=str, default="/tmp/v7_grid_search.json", help="output JSON")
    args = parser.parse_args()

    print("=" * 80)
    print(f"V7 Spreading Activation Grid Search")
    print("=" * 80)
    print(f"Grid: decay {DECAY_GRID} × threshold {THRESHOLD_GRID} × max_hops {MAX_HOPS_GRID}")
    print(f"     = {len(DECAY_GRID) * len(THRESHOLD_GRID) * len(MAX_HOPS_GRID)} configurations")
    print(f"Per config: n_queries={args.n_queries}, K={args.k}")
    print()

    # 连接 + 加载
    print("Connecting to PG...")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB)
    print(f"Loading graph from {SCHEMA}...")
    graph = load_graph(conn, SCHEMA)
    print(f"  Graph: {len(graph)} nodes, {sum(len(v) for v in graph.values()) // 2} edges")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT id, importance FROM {SCHEMA}.memory_chunks")
    importance_map = {r['id']: r['importance'] for r in cur.fetchall()}
    cur.close()
    print(f"Loading {args.n_queries} seeds...")
    seeds_data = load_seed_chunks(conn, SCHEMA, n=args.n_queries, seed=args.seed)
    print(f"  Seeds loaded: {len(seeds_data)}")
    print()

    # 跑 BFS baseline (跟 PoC-1 一致)
    print("Running BFS baseline...")
    bfs_result = run_bfs_baseline(seeds_data, graph, importance_map, k=args.k)
    print(f"  BFS: composite={bfs_result['composite']:.4f}, "
          f"α={bfs_result['adjacency']:.4f}, β={bfs_result['significance']:.4f}, γ={bfs_result['breadth']:.4f}")
    print()

    # Grid search
    print("Running grid search...")
    results = []
    total_configs = len(DECAY_GRID) * len(THRESHOLD_GRID) * len(MAX_HOPS_GRID)
    i = 0
    t0_total = time.perf_counter()
    for decay, threshold, max_hops in itertools.product(DECAY_GRID, THRESHOLD_GRID, MAX_HOPS_GRID):
        i += 1
        t0 = time.perf_counter()
        sa = run_one_config(seeds_data, graph, importance_map, decay, threshold, max_hops, k=args.k)
        dt = time.perf_counter() - t0
        if sa is None:
            print(f"  [{i}/{total_configs}] d={decay} t={threshold} h={max_hops}: SKIPPED (no valid seeds)")
            continue
        delta = sa['composite'] - bfs_result['composite']
        pass_this = delta >= 0.15
        results.append({
            "decay": decay, "threshold": threshold, "max_hops": max_hops,
            "composite": sa['composite'],
            "adjacency": sa['adjacency'],
            "significance": sa['significance'],
            "breadth": sa['breadth'],
            "delta_vs_bfs": delta,
            "pass": pass_this,
            "latency_s": dt,
            "n_evaluated": sa['n_evaluated'],
        })
        mark = "✅" if pass_this else "❌"
        print(f"  [{i}/{total_configs}] d={decay} t={threshold} h={max_hops}: "
              f"comp={sa['composite']:.4f} Δ={delta:+.4f} {mark} ({dt:.2f}s)")

    total_dt = time.perf_counter() - t0_total
    print(f"\nGrid search done in {total_dt:.1f}s")
    print()

    # 排序找最优
    results_sorted = sorted(results, key=lambda r: -r['composite'])
    best = results_sorted[0]

    # 报告
    print("=" * 80)
    print("GRID SEARCH RESULTS (sorted by composite)")
    print("=" * 80)
    print(f"{'rank':<5} {'decay':<7} {'thresh':<7} {'max_hops':<9} {'composite':<11} "
          f"{'Δ vs BFS':<11} {'α':<7} {'β':<7} {'γ':<7} {'PASS':<6}")
    print("-" * 80)
    for rank, r in enumerate(results_sorted, 1):
        mark = "✅" if r['pass'] else "❌"
        print(f"{rank:<5} {r['decay']:<7} {r['threshold']:<7} {r['max_hops']:<9} "
              f"{r['composite']:<11.4f} {r['delta_vs_bfs']:<+11.4f} "
              f"{r['adjacency']:<7.3f} {r['significance']:<7.3f} {r['breadth']:<7.3f} {mark}")
    print("-" * 80)
    print(f"BFS baseline: composite={bfs_result['composite']:.4f}")
    print()

    # 决策
    print("=" * 80)
    print("GO/NO-GO DECISION (per ADR-074 PoC 标准)")
    print("=" * 80)
    print(f"  最优参数: decay={best['decay']}, threshold={best['threshold']}, max_hops={best['max_hops']}")
    print(f"  最优 composite: {best['composite']:.4f}")
    print(f"  BFS composite: {bfs_result['composite']:.4f}")
    print(f"  Δ: {best['delta_vs_bfs']:+.4f} (need ≥ +0.15)")
    if best['pass']:
        print(f"  {'✅ PASS — proceed to Sprint 1 implementation'}")
    else:
        print(f"  {'❌ FAIL — V7 spread activation NOT recommended, archive'}")
    print("=" * 80)

    # 写输出
    output = {
        "config": vars(args),
        "bfs_baseline": bfs_result,
        "grid_results": results_sorted,
        "best": best,
        "pass_overall": best['pass'],
        "n_configs_passing": sum(1 for r in results if r['pass']),
    }
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nResults saved: {args.output}")

    conn.close()
    sys.exit(0 if best['pass'] else 1)


if __name__ == "__main__":
    main()
