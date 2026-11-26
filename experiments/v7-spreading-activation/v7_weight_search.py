"""V7 composite weight sensitivity analysis (ADR-074 weight revision).

问题: 当前 composite 公式 α=0.4 + β=0.3 + γ=0.3,α 主导导致 BFS 赢.
      真实数据 grid search 27 组全 FAIL.

本脚本: 跑多组权重组合, 看权重改后 SA 能不能赢.
诚实标注: 改权重 = 改 PASS 门槛, 即使 PASS 也不等于 "SA 真好",
          只是说明 "在改后的指标下 SA 赢".

权重候选 (per user B 选项):
  w1 (current):  α=0.4, β=0.3, γ=0.3
  w2 (reduce α):  α=0.2, β=0.4, γ=0.4
  w3 (extreme):   α=0.1, β=0.5, γ=0.4
  w4 (no α):      α=0.0, β=0.5, γ=0.5
  w5 (β-dominant): α=0.2, β=0.6, γ=0.2

每组权重:
  1. 用 grid search 27 组 SA 找最优 SA 参数
  2. 用同权重算 BFS composite (也用同样权重算 BFS,保证对比公平)
  3. 对比最优 SA vs BFS
"""

import argparse
import json
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "src"))

from spreading_activation import spread_activation, _bfs_recall
from v7_evaluate_real import (
    load_graph, load_seed_chunks,
    adj_metric, sig_metric, breadth_metric,
    PG_HOST, PG_PORT, PG_USER, PG_PASS, PG_DB, SCHEMA,
)


# 权重候选
WEIGHT_CONFIGS = [
    ("w1_current",     {"adjacency": 0.4, "significance": 0.3, "breadth": 0.3}),
    ("w2_reduce_alpha", {"adjacency": 0.2, "significance": 0.4, "breadth": 0.4}),
    ("w3_extreme",     {"adjacency": 0.1, "significance": 0.5, "breadth": 0.4}),
    ("w4_no_alpha",    {"adjacency": 0.0, "significance": 0.5, "breadth": 0.5}),
    ("w5_beta_dominant", {"adjacency": 0.2, "significance": 0.6, "breadth": 0.2}),
]


def composite(metrics, weights):
    """按权重算 composite."""
    return (
        weights["adjacency"] * metrics["adjacency"]
        + weights["significance"] * metrics["significance"]
        + weights["breadth"] * metrics["breadth"]
    )


def evaluate_with_weights(seeds_data, graph, importance_map, algo_fn, algo_kwargs, weights, k=10):
    """跑一个算法 (SA 或 BFS), 用给定权重算 composite."""
    adj_list, sig_list, brd_list, comp_list = [], [], [], []
    skipped = 0
    for sc in seeds_data:
        seed = sc["id"]
        if not seed or seed not in graph:
            skipped += 1
            continue
        try:
            ids = algo_fn(seed, graph, **algo_kwargs)
            ids = [c for c in ids if c != seed][:k]
            if not ids:
                skipped += 1
                continue
            a = adj_metric(ids, seed, graph)
            s = sig_metric(ids, importance_map)
            b = breadth_metric(ids)
            c = composite({"adjacency": a, "significance": s, "breadth": b}, weights)
            adj_list.append(a); sig_list.append(s); brd_list.append(b); comp_list.append(c)
        except Exception:
            skipped += 1
    if not comp_list:
        return None
    n = len(comp_list)
    return {
        "adjacency": sum(adj_list) / n,
        "significance": sum(sig_list) / n,
        "breadth": sum(brd_list) / n,
        "composite": sum(comp_list) / n,
        "n_evaluated": n,
        "skipped": skipped,
    }


def sa_algo(seed, graph, **kwargs):
    """SA algo wrapper."""
    result = spread_activation([seed], graph, **kwargs)
    return [cid for cid, _ in result]


def bfs_algo(seed, graph, **kwargs):
    """BFS algo wrapper."""
    return _bfs_recall([seed], graph, min_strength=0.5, cap=6)


def main():
    parser = argparse.ArgumentParser(description="V7 weight sensitivity")
    parser.add_argument("--n-queries", type=int, default=100, help="seed queries")
    parser.add_argument("--k", type=int, default=10, help="top-K")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--output", type=str, default="/tmp/v7_weight_search.json", help="output JSON")
    args = parser.parse_args()

    print("=" * 80)
    print("V7 Composite Weight Sensitivity Analysis")
    print("=" * 80)
    print(f"Configs: {len(WEIGHT_CONFIGS)} weight combinations")
    print(f"Per config: n_queries={args.n_queries}, K={args.k}")
    print()

    # 连接 + 加载
    print("Connecting to PG...")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB)
    graph = load_graph(conn, SCHEMA)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT id, importance FROM {SCHEMA}.memory_chunks")
    importance_map = {r['id']: r['importance'] for r in cur.fetchall()}
    cur.close()
    seeds_data = load_seed_chunks(conn, SCHEMA, n=args.n_queries, seed=args.seed)
    print(f"  Graph: {len(graph)} nodes, seeds: {len(seeds_data)}")
    print()

    # Grid search SA 参数 (一次跑,所有权重组共用)
    DECAY_GRID = [0.5, 0.7, 0.9]
    THRESHOLD_GRID = [0.01, 0.05, 0.1]
    MAX_HOPS_GRID = [2, 3, 4]

    print("Pre-computing SA grid (27 configs × 100 seeds)...")
    t0 = time.perf_counter()
    sa_grid = {}  # (d, t, h) -> [per-seed metric dicts]
    for decay, threshold, max_hops in [(d, t, h) for d in DECAY_GRID for t in THRESHOLD_GRID for h in MAX_HOPS_GRID]:
        per_seed = []
        for sc in seeds_data:
            seed = sc["id"]
            if not seed or seed not in graph:
                continue
            try:
                sa_result = spread_activation([seed], graph, decay=decay, threshold=threshold, max_hops=max_hops)
                sa_ids = [cid for cid, _ in sa_result if cid != seed][:args.k]
                if not sa_ids:
                    continue
                a = adj_metric(sa_ids, seed, graph)
                s = sig_metric(sa_ids, importance_map)
                b = breadth_metric(sa_ids)
                per_seed.append({"adjacency": a, "significance": s, "breadth": b, "ids": sa_ids})
            except Exception:
                continue
        sa_grid[(decay, threshold, max_hops)] = per_seed
    print(f"  SA grid computed in {time.perf_counter() - t0:.1f}s")
    print()

    # BFS pre-compute (per seed)
    print("Pre-computing BFS (per seed)...")
    bfs_per_seed = []
    for sc in seeds_data:
        seed = sc["id"]
        if not seed or seed not in graph:
            continue
        try:
            bfs_ids = _bfs_recall([seed], graph, min_strength=0.5, cap=6)[:args.k]
            if not bfs_ids:
                continue
            a = adj_metric(bfs_ids, seed, graph)
            s = sig_metric(bfs_ids, importance_map)
            b = breadth_metric(bfs_ids)
            bfs_per_seed.append({"adjacency": a, "significance": s, "breadth": b, "ids": bfs_ids})
        except Exception:
            continue
    print(f"  BFS computed: {len(bfs_per_seed)} seeds")
    print()

    # 权重分析
    results_by_weight = {}
    overall_pass = False  # 任一权重组 pass 就 True

    for wname, weights in WEIGHT_CONFIGS:
        print(f"--- Weight: {wname} (α={weights['adjacency']}, β={weights['significance']}, γ={weights['breadth']}) ---")

        # BFS composite (此权重下)
        bfs_composites = [composite(m, weights) for m in bfs_per_seed]
        bfs_avg = sum(bfs_composites) / len(bfs_composites) if bfs_composites else 0.0

        # SA grid: 找最优参数 (按此权重的 composite)
        sa_results = []
        for (d, t, h), per_seed in sa_grid.items():
            comps = [composite(m, weights) for m in per_seed]
            if not comps:
                continue
            sa_avg = sum(comps) / len(comps)
            delta = sa_avg - bfs_avg
            sa_results.append({
                "decay": d, "threshold": t, "max_hops": h,
                "composite": sa_avg,
                "delta": delta,
                "pass": delta >= 0.15,
            })

        sa_results.sort(key=lambda r: -r["composite"])
        best = sa_results[0] if sa_results else None

        pass_this = best is not None and best["pass"]
        if pass_this:
            overall_pass = True

        print(f"  BFS composite: {bfs_avg:.4f}")
        print(f"  Best SA: d={best['decay']} t={best['threshold']} h={best['max_hops']} "
              f"composite={best['composite']:.4f} Δ={best['delta']:+.4f}")
        print(f"  {'✅ PASS' if pass_this else '❌ FAIL'}")
        print(f"  All 27 SA configs:")
        for r in sa_results[:5]:
            mark = "✅" if r["pass"] else "❌"
            print(f"    {mark} d={r['decay']} t={r['threshold']} h={r['max_hops']}: "
                  f"composite={r['composite']:.4f} Δ={r['delta']:+.4f}")
        if len(sa_results) > 5:
            print(f"    ... ({len(sa_results) - 5} more)")
        print()

        results_by_weight[wname] = {
            "weights": weights,
            "bfs_composite": bfs_avg,
            "sa_best": best,
            "sa_all": sa_results,
            "n_passing": sum(1 for r in sa_results if r["pass"]),
        }

    print("=" * 80)
    print("WEIGHT SENSITIVITY SUMMARY")
    print("=" * 80)
    print(f"{'weight':<22} {'BFS':<8} {'Best SA':<10} {'Δ':<8} {'PASS':<6}")
    print("-" * 80)
    for wname in [w[0] for w in WEIGHT_CONFIGS]:
        r = results_by_weight[wname]
        bfs_v = r["bfs_composite"]
        sa_v = r["sa_best"]["composite"] if r["sa_best"] else 0
        delta = r["sa_best"]["delta"] if r["sa_best"] else 0
        mark = "✅" if r["sa_best"] and r["sa_best"]["pass"] else "❌"
        print(f"{wname:<22} {bfs_v:<8.4f} {sa_v:<10.4f} {delta:<+8.4f} {mark}")
    print()

    # 诚实标注
    print("=" * 80)
    print("⚠️ 诚实归因 (per user principle)")
    print("=" * 80)
    print("改权重 = 改 PASS 门槛. 即使在某些权重下 SA PASS, 也只说明:")
    print("  '在该权重设计下, SA 在 cortex 真实数据上优于 BFS.'")
    print("  不等于 'SA 在客观意义上更好'.")
    print()
    if overall_pass:
        print(f"✅ 至少 1 个权重组下 SA 显著赢 BFS (Δ ≥ +15%)")
        print(f"   → 需要 ADR-074 修订 + 重新评估 '什么是好的 composite 权重'")
    else:
        print(f"❌ 所有权重组 SA 都未显著赢 BFS")
        print(f"   → V7 spread activation 在 cortex V5/V6 schema 下确认不推荐")

    # 写输出
    output = {
        "config": vars(args),
        "weight_configs": [{"name": wn, "weights": wd} for wn, wd in WEIGHT_CONFIGS],
        "results_by_weight": {
            wn: {
                "weights": r["weights"],
                "bfs_composite": r["bfs_composite"],
                "sa_best": r["sa_best"],
                "n_passing": r["n_passing"],
            }
            for wn, r in results_by_weight.items()
        },
        "overall_pass_any_weight": overall_pass,
        "honest_caveat": "改权重 = 改测试规则. 即使 PASS 也只说明 '在新指标下 SA 赢', 不等于 'SA 客观更好'.",
    }
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nResults saved: {args.output}")

    conn.close()
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
