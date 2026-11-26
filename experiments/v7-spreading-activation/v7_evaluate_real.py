"""V7 real-data evaluation: 用真实 2230 chunks + 1483 relations 跑 SA vs BFS.

真实数据无 ground truth, 用 3 个启发式指标:
  α 邻接性: SA top-10 中跟 seed 有 memory_relations 边的比例
  β 显著性: SA top-10 中 importance ≥ 7 的比例
  γ 广度: SA top-10 中 unique chunks 数 (去重后)

综合得分 (δ): α * 0.4 + β * 0.3 + γ * 0.3

PASS 标准 (与 PoC-1 一致): SA 综合得分 ≥ BFS + 15%

queries 来源: 随机选 100 个 high-importance chunk 作为 seed (importance ≥ 7)
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))  # experiments/v7-spreading-activation/

from spreading_activation import spread_activation, _bfs_recall

PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "postgres"
PG_PASS = "change_me"
PG_DB = "postgres"
SCHEMA = "cortex_v7_dev"


def load_graph(conn, schema: str) -> dict:
    """从 DB 加载邻接表 {chunk_id: {neighbor_id: edge_weight}}."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT memory_id_1, memory_id_2, strength FROM {schema}.memory_relations WHERE strength > 0")
    graph = {}
    for r in cur.fetchall():
        a, b, w = r['memory_id_1'], r['memory_id_2'], float(r['strength'])
        graph.setdefault(a, {})[b] = w
        graph.setdefault(b, {})[a] = w  # undirected
    cur.close()
    return graph


def load_seed_chunks(conn, schema: str, n: int = 100, seed: int = 42) -> list:
    """随机选 N 个 high-importance chunk 作为 query seed."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT id, importance, type FROM {schema}.memory_chunks
        WHERE importance >= 7
        ORDER BY random()
        LIMIT %s
    """, (n,))
    rows = cur.fetchall()
    cur.close()
    random.seed(seed)
    return rows


def adj_metric(retrieved: list, seed: str, graph: dict) -> float:
    """α 邻接性: top-10 中跟 seed 有直接边的比例."""
    if not retrieved:
        return 0.0
    seed_neighbors = set(graph.get(seed, {}).keys())
    if not seed_neighbors:
        return 0.0
    hits = sum(1 for r in retrieved if r in seed_neighbors)
    return hits / len(retrieved)


def sig_metric(retrieved: list, importance_map: dict) -> float:
    """β 显著性: top-10 中 importance ≥ 7 的比例."""
    if not retrieved:
        return 0.0
    hits = sum(1 for r in retrieved if importance_map.get(r, 0) >= 7)
    return hits / len(retrieved)


def breadth_metric(retrieved: list) -> float:
    """γ 广度: unique chunks (始终是 retrieved 长度 / 期望, 归一化到 [0,1])."""
    if not retrieved:
        return 0.0
    # 广度: 实际 distinct 数 / 期望最大 (K=10)
    return len(set(retrieved)) / min(10, len(retrieved))


def evaluate_query(seeds: list, graph: dict, importance_map: dict, k: int = 10) -> dict:
    """跑 SA + BFS 对 1 个 seed, 算所有指标."""
    seed = seeds[0] if seeds else None
    if not seed or seed not in graph:
        return {"seed": seed, "sa": None, "bfs": None, "skip_reason": "no_seed_or_no_neighbors"}

    sa_result = spread_activation([seed], graph)
    sa_ids = [cid for cid, _ in sa_result if cid != seed][:k]

    bfs_result = _bfs_recall([seed], graph, min_strength=0.5, cap=6)
    bfs_ids = bfs_result[:k]

    sa_metrics = {
        "adjacency": adj_metric(sa_ids, seed, graph),
        "significance": sig_metric(sa_ids, importance_map),
        "breadth": breadth_metric(sa_ids),
        "top_ids": sa_ids,
    }
    bfs_metrics = {
        "adjacency": adj_metric(bfs_ids, seed, graph),
        "significance": sig_metric(bfs_ids, importance_map),
        "breadth": breadth_metric(bfs_ids),
        "top_ids": bfs_ids,
    }
    sa_metrics["composite"] = 0.4 * sa_metrics["adjacency"] + 0.3 * sa_metrics["significance"] + 0.3 * sa_metrics["breadth"]
    bfs_metrics["composite"] = 0.4 * bfs_metrics["adjacency"] + 0.3 * bfs_metrics["significance"] + 0.3 * bfs_metrics["breadth"]

    return {"seed": seed, "sa": sa_metrics, "bfs": bfs_metrics}


def main():
    parser = argparse.ArgumentParser(description="V7 real-data SA vs BFS")
    parser.add_argument("--n-queries", type=int, default=100, help="number of seed queries")
    parser.add_argument("--k", type=int, default=10, help="top-K")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--output", type=str, default="/tmp/v7_real_results.json", help="output JSON path")
    args = parser.parse_args()

    print("=" * 70)
    print(f"V7 Real-Data Evaluation: SA vs BFS")
    print("=" * 70)

    print(f"\nConnecting to PG...")
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB)

    print(f"Loading graph from {SCHEMA}...")
    graph = load_graph(conn, SCHEMA)
    print(f"  Graph: {len(graph)} nodes, {sum(len(v) for v in graph.values()) // 2} edges")

    print(f"Loading importance map...")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT id, importance FROM {SCHEMA}.memory_chunks")
    importance_map = {r['id']: r['importance'] for r in cur.fetchall()}
    cur.close()

    print(f"Loading {args.n_queries} seed queries (importance ≥ 7)...")
    seed_chunks = load_seed_chunks(conn, SCHEMA, n=args.n_queries, seed=args.seed)
    print(f"  Seeds loaded: {len(seed_chunks)}")

    # 跑评估
    print(f"\nRunning evaluation (SA + BFS)...")
    sa_adj, sa_sig, sa_brd, sa_comp = [], [], [], []
    bfs_adj, bfs_sig, bfs_brd, bfs_comp = [], [], [], []
    skipped = 0

    for i, sc in enumerate(seed_chunks):
        result = evaluate_query([sc['id']], graph, importance_map, k=args.k)
        if result.get("skip_reason"):
            skipped += 1
            continue
        sa = result['sa']
        bfs = result['bfs']
        sa_adj.append(sa['adjacency']); sa_sig.append(sa['significance']); sa_brd.append(sa['breadth']); sa_comp.append(sa['composite'])
        bfs_adj.append(bfs['adjacency']); bfs_sig.append(bfs['significance']); bfs_brd.append(bfs['breadth']); bfs_comp.append(bfs['composite'])

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    sa_avg = {
        "adjacency": avg(sa_adj),
        "significance": avg(sa_sig),
        "breadth": avg(sa_brd),
        "composite": avg(sa_comp),
    }
    bfs_avg = {
        "adjacency": avg(bfs_adj),
        "significance": avg(bfs_sig),
        "breadth": avg(bfs_brd),
        "composite": avg(bfs_comp),
    }

    print()
    print("-" * 70)
    print(f"{'Metric':<25} {'SA':>15} {'BFS':>15} {'Δ':>10}")
    print("-" * 70)
    for name, label in [("adjacency", "α adjacency"), ("significance", "β significance"),
                         ("breadth", "γ breadth"), ("composite", "δ COMPOSITE")]:
        sa_v = sa_avg[name]
        bfs_v = bfs_avg[name]
        delta = sa_v - bfs_v
        print(f"{label:<25} {sa_v:>15.4f} {bfs_v:>15.4f} {delta:>+10.4f}")
    print("-" * 70)
    print(f"Skipped: {skipped} / {len(seed_chunks)} (no neighbors)")
    print()

    # PASS/FAIL
    delta_composite = sa_avg['composite'] - bfs_avg['composite']
    pass_main = delta_composite >= 0.15

    print("=" * 70)
    print("PASS Verdict (composite ≥ BFS + 0.15)")
    print("=" * 70)
    print(f"  SA composite  = {sa_avg['composite']:.4f}")
    print(f"  BFS composite = {bfs_avg['composite']:.4f}")
    print(f"  Δ             = {delta_composite:+.4f} (need ≥ +0.15)")
    print(f"  {'✅ PASS' if pass_main else '❌ FAIL'}")
    print("=" * 70)

    # 写输出
    output = {
        "config": vars(args),
        "schema": SCHEMA,
        "graph_stats": {"nodes": len(graph), "edges": sum(len(v) for v in graph.values()) // 2},
        "results": {
            "sa": sa_avg,
            "bfs": bfs_avg,
            "delta_composite": delta_composite,
            "pass": pass_main,
            "skipped": skipped,
            "total_queries": len(seed_chunks),
        },
    }
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nResults saved: {args.output}")

    conn.close()
    sys.exit(0 if pass_main else 1)


if __name__ == "__main__":
    main()
