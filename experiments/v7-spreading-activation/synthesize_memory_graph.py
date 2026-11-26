"""Synthesize a memory graph for PoC-1 verification (ADR-074).

Generates 100 chunks across 10 topic clusters with controlled edge density.
Each cluster has known ground truth relationships for recall evaluation.

Output: JSON file with:
  - chunks: list of {id, content, type, importance, cluster_id, tags}
  - edges: list of {from, to, weight, edge_type}
  - queries: list of {seed_ids, ground_truth_ids, cluster_id}

Usage:
    python scripts/synthesize_memory_graph.py --chunks 100 --output /tmp/poc_data.json
"""

import argparse
import json
import random
import sys
from pathlib import Path


def synthesize(n_chunks: int = 100, n_clusters: int = 10, seed: int = 42) -> dict:
    """Generate synthetic memory graph with controlled structure.

    Structure:
    - n_clusters clusters, each ~n_chunks/n_clusters chunks
    - Within-cluster edge density = 0.8 (high)
    - Cross-cluster edge density = 0.1 (low)
    - Edge weights: 0.3-1.0 uniform
    """
    random.seed(seed)

    chunks_per_cluster = n_chunks // n_clusters
    assert chunks_per_cluster >= 2, "need >= 2 chunks per cluster"

    # Generate chunks
    chunks = []
    cluster_map = {}  # chunk_id -> cluster_id
    for c in range(n_clusters):
        for i in range(chunks_per_cluster):
            cid = f"c{c}_n{i}"
            chunks.append({
                "id": cid,
                "content": f"Topic {c} note {i}: synthetic content for cluster {c}",
                "type": "synthetic",
                "importance": random.randint(3, 9),
                "cluster_id": c,
                "tags": [f"topic_{c}", f"subtopic_{i % 3}"],
            })
            cluster_map[cid] = c

    # Generate edges
    edges = []
    for i, c1 in enumerate(chunks):
        for j, c2 in enumerate(chunks):
            if i >= j:
                continue  # avoid duplicates + self-loops
            same_cluster = cluster_map[c1["id"]] == cluster_map[c2["id"]]
            edge_prob = 0.8 if same_cluster else 0.1
            if random.random() < edge_prob:
                weight = round(random.uniform(0.3, 1.0), 3)
                edge_type = "intra_cluster" if same_cluster else "cross_cluster"
                edges.append({
                    "from": c1["id"],
                    "to": c2["id"],
                    "weight": weight,
                    "edge_type": edge_type,
                })

    # Generate queries: pick 1-2 seed chunks per cluster, ground truth = other chunks in same cluster
    queries = []
    for c in range(n_clusters):
        cluster_chunks = [ch["id"] for ch in chunks if ch["cluster_id"] == c]
        # 5 queries per cluster
        for q in range(5):
            n_seeds = random.choice([1, 2])
            seeds = random.sample(cluster_chunks, n_seeds)
            # Ground truth = other chunks in same cluster (excludes seeds)
            ground_truth = [cid for cid in cluster_chunks if cid not in seeds]
            queries.append({
                "query_id": f"q_c{c}_{q}",
                "seeds": seeds,
                "ground_truth": ground_truth,
                "cluster_id": c,
            })

    return {
        "meta": {
            "n_chunks": len(chunks),
            "n_edges": len(edges),
            "n_clusters": n_clusters,
            "n_queries": len(queries),
            "intra_density": 0.8,
            "cross_density": 0.1,
        },
        "chunks": chunks,
        "edges": edges,
        "queries": queries,
    }


def main():
    parser = argparse.ArgumentParser(description="Synthesize memory graph for V7 PoC-1")
    parser.add_argument("--chunks", type=int, default=100, help="total chunk count (default 100)")
    parser.add_argument("--clusters", type=int, default=10, help="cluster count (default 10)")
    parser.add_argument("--seed", type=int, default=42, help="random seed for reproducibility")
    parser.add_argument("--output", type=str, required=True, help="output JSON file path")
    args = parser.parse_args()

    data = synthesize(args.chunks, args.clusters, args.seed)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Generated synthetic memory graph:")
    print(f"  chunks: {data['meta']['n_chunks']}")
    print(f"  edges: {data['meta']['n_edges']}")
    print(f"  clusters: {data['meta']['n_clusters']}")
    print(f"  queries: {data['meta']['n_queries']}")
    print(f"  intra-cluster density: {data['meta']['intra_density']}")
    print(f"  cross-cluster density: {data['meta']['cross_density']}")
    print(f"  output: {output_path}")
    print(f"  size: {output_path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
