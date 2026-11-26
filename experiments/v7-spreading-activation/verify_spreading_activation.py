"""V7 PoC-1 verification: compare Spreading Activation vs BFS on synthetic data.

Per ADR-074:
- Pass criteria: recall@10 (SA) >= recall@10 (BFS) + 15%
- Side criteria: latency, precision, robustness

Usage:
    python scripts/verify_spreading_activation.py --data /tmp/poc_data.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

# 把 server/src 加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "src"))

from spreading_activation import spread_activation, _bfs_recall


def build_graph(data: dict) -> dict:
    """Convert edges list to adjacency map {from: {to: weight}}."""
    graph = {}
    for edge in data["edges"]:
        f, t, w = edge["from"], edge["to"], edge["weight"]
        graph.setdefault(f, {})[t] = w
        graph.setdefault(t, {})[f] = w  # undirected
    return graph


def compute_recall_at_k(retrieved: list, ground_truth: list, k: int) -> float:
    """recall@k = |retrieved[:k] ∩ ground_truth| / |ground_truth|."""
    if not ground_truth:
        return 0.0
    gt_set = set(ground_truth)
    retrieved_set = set(retrieved[:k])
    return len(retrieved_set & gt_set) / len(gt_set)


def compute_precision_at_k(retrieved: list, ground_truth: list, k: int) -> float:
    """precision@k = |retrieved[:k] ∩ ground_truth| / k."""
    if k == 0:
        return 0.0
    gt_set = set(ground_truth)
    retrieved_set = set(retrieved[:k])
    return len(retrieved_set & gt_set) / k


def run_evaluation(data: dict, k_eval: int = 10) -> dict:
    """Run both algorithms on all queries, compute aggregate metrics."""
    graph = build_graph(data)
    queries = data["queries"]

    sa_recalls = []
    sa_precisions = []
    sa_latencies = []
    bfs_recalls = []
    bfs_precisions = []
    bfs_latencies = []
    failures = 0

    for q in queries:
        seeds = q["seeds"]
        gt = q["ground_truth"]

        # SA evaluation
        try:
            t0 = time.perf_counter()
            sa_results = spread_activation(seeds, graph)
            sa_latency_ms = (time.perf_counter() - t0) * 1000
            sa_ids = [cid for cid, _ in sa_results]
            sa_recalls.append(compute_recall_at_k(sa_ids, gt, k_eval))
            sa_precisions.append(compute_precision_at_k(sa_ids, gt, k_eval))
            sa_latencies.append(sa_latency_ms)
        except Exception as e:
            failures += 1
            print(f"  SA failed on {q['query_id']}: {e}")

        # BFS evaluation
        try:
            t0 = time.perf_counter()
            # Fair comparison: cap=None means unlimited BFS (same scan depth as SA)
            bfs_results = _bfs_recall(seeds, graph, min_strength=0.0, cap=None)
            bfs_latency_ms = (time.perf_counter() - t0) * 1000
            bfs_recalls.append(compute_recall_at_k(bfs_results, gt, k_eval))
            bfs_precisions.append(compute_precision_at_k(bfs_results, gt, k_eval))
            bfs_latencies.append(bfs_latency_ms)
        except Exception as e:
            failures += 1
            print(f"  BFS failed on {q['query_id']}: {e}")

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    return {
        "n_queries": len(queries),
        "failures": failures,
        "sa": {
            "recall@k": avg(sa_recalls),
            "precision@k": avg(sa_precisions),
            "latency_ms": avg(sa_latencies),
        },
        "bfs": {
            "recall@k": avg(bfs_recalls),
            "precision@k": avg(bfs_precisions),
            "latency_ms": avg(bfs_latencies),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Verify spreading activation vs BFS")
    parser.add_argument("--data", type=str, required=True, help="input JSON from synthesize_memory_graph.py")
    parser.add_argument("--k", type=int, default=10, help="K for recall@K / precision@K (default 10)")
    args = parser.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    print("=" * 70)
    print(f"V7 PoC-1 Verification: Spreading Activation vs BFS")
    print("=" * 70)
    print(f"Data: {data['meta']['n_chunks']} chunks, {data['meta']['n_edges']} edges, "
          f"{data['meta']['n_clusters']} clusters, {data['meta']['n_queries']} queries")
    print(f"K = {args.k} for recall@K / precision@K")
    print()

    result = run_evaluation(data, k_eval=args.k)

    sa = result["sa"]
    bfs = result["bfs"]

    print("-" * 70)
    print(f"{'Metric':<25} {'SA':>15} {'BFS':>15} {'Δ':>10}")
    print("-" * 70)
    print(f"{'recall@' + str(args.k):<25} {sa['recall@k']:>15.4f} {bfs['recall@k']:>15.4f} "
          f"{(sa['recall@k'] - bfs['recall@k']):>+10.4f}")
    print(f"{'precision@' + str(args.k):<25} {sa['precision@k']:>15.4f} {bfs['precision@k']:>15.4f} "
          f"{(sa['precision@k'] - bfs['precision@k']):>+10.4f}")
    print(f"{'latency (ms)':<25} {sa['latency_ms']:>15.4f} {bfs['latency_ms']:>15.4f} "
          f"{(sa['latency_ms'] - bfs['latency_ms']):>+10.4f}")
    print("-" * 70)
    print(f"Failures: {result['failures']} / {result['n_queries']}")
    print()

    # Pass/fail evaluation per ADR-074
    recall_improvement = sa['recall@k'] - bfs['recall@k']
    precision_drop = bfs['precision@k'] - sa['precision@k']
    latency_ratio = sa['latency_ms'] / bfs['latency_ms'] if bfs['latency_ms'] > 0 else float('inf')

    print("=" * 70)
    print("PoC-1 Verdict (per ADR-074 §PoC-1 通过标准)")
    print("=" * 70)

    # Main criterion
    main_pass = recall_improvement >= 0.15
    print(f"  {'✅' if main_pass else '❌'} Main: SA recall@K ≥ BFS + 15%")
    print(f"     SA = {sa['recall@k']:.4f}, BFS = {bfs['recall@k']:.4f}, "
          f"improvement = {recall_improvement:+.4f} (need ≥ +0.15)")

    # Side criteria
    side1 = latency_ratio <= 3.0
    print(f"  {'✅' if side1 else '❌'} Side-1: latency ≤ BFS × 3")
    print(f"     SA = {sa['latency_ms']:.4f} ms, BFS = {bfs['latency_ms']:.4f} ms, "
          f"ratio = {latency_ratio:.2f}x (need ≤ 3x)")

    side2 = precision_drop <= 0.05
    print(f"  {'✅' if side2 else '❌'} Side-2: precision drop ≤ 5%")
    print(f"     SA = {sa['precision@k']:.4f}, BFS = {bfs['precision@k']:.4f}, "
          f"drop = {precision_drop:+.4f} (need ≥ -0.05)")

    side3 = result['failures'] == 0
    print(f"  {'✅' if side3 else '❌'} Side-3: no failures across all queries")
    print(f"     {result['failures']} / {result['n_queries']} queries failed")

    print()
    overall_pass = main_pass and side1 and side2 and side3
    print(f"{'='*70}")
    print(f"OVERALL: {'✅ PASS — proceed to Sprint 1' if overall_pass else '❌ FAIL — V7 spread activation NOT recommended'}")
    print(f"{'='*70}")

    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
