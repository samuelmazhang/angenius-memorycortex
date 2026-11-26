"""Personalized PageRank (PPR) -- pure-stdlib power iteration.

Used by ``include_related=true`` recall to promote neighbors that are
"topically close" to the query hits, rather than relying on a flat
strength threshold. The implementation is intentionally dependency-free
(no networkx, no scipy) so it runs in the cold-start FastAPI process and
ships in the unit-test suite without extra installs.

Reference: HippoRAG (Gutiérrez et al., NeurIPS 2024) -- the anchor set is
the union of lexical + vector hit chunk ids, the personalization vector
is uniform over those anchors, and α=0.85 is the standard damping factor.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .index import GraphIndex


def personalized_pagerank(
    graph: GraphIndex,
    seeds: Sequence[str],
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
    dangling: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Run personalized PageRank over ``graph`` seeded at ``seeds``.

    Args:
        graph: The adjacency view.
        seeds: Anchor node ids. The personalization vector is uniform
            over the seed set; duplicates are deduped automatically.
        alpha: Damping factor (probability of following an edge).
        max_iter: Hard cap on iterations.
        tol: L1 convergence threshold (early-stop when the diff drops below).
        dangling: Optional {node_id: weight} mass distribution for nodes
            with no outgoing edges (the standard "random surfer" teleports
            to seeds, but for graphs with isolated leaves this lets the
            caller redirect that mass). When ``None``, dangling mass
            teleports back to the seeds (same as the seed vector).

    Returns:
        ``{node_id: score}`` mapping. Nodes never visited return 0.0.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1); got {alpha}")
    if max_iter <= 0:
        raise ValueError(f"max_iter must be > 0; got {max_iter}")
    if tol <= 0:
        raise ValueError(f"tol must be > 0; got {tol}")

    nodes = graph.nodes()
    if not nodes or not seeds:
        return {}

    # --- personalization vector (uniform over unique seeds) ---
    seed_set: Set[str] = {s for s in seeds if s in nodes}
    if not seed_set:
        # seeds are all outside the graph -- fall back to uniform over nodes
        seed_set = set(nodes)
    inv_n = 1.0 / len(seed_set)
    personalization: Dict[str, float] = {n: 0.0 for n in nodes}
    for s in seed_set:
        personalization[s] = inv_n

    # --- per-node out-weight (sum of outgoing edge weights) ---
    out_weight: Dict[str, float] = {n: graph.weighted_degree(n) for n in nodes}

    # --- dangling nodes: out_weight == 0 ---
    dangling_nodes: List[str] = [n for n in nodes if out_weight[n] <= 0]

    # --- initial score = personalization (some implementations start at 0,
    #     but seeding with personalization converges in fewer iterations) ---
    scores: Dict[str, float] = dict(personalization)
    scores_next: Dict[str, float] = {n: 0.0 for n in nodes}

    for iteration in range(max_iter):
        # 1) dangling redistribution
        if dangling_nodes:
            if dangling is None:
                # teleport to seeds
                dangling_mass = sum(scores[n] for n in dangling_nodes)
                for s in seed_set:
                    scores_next[s] += alpha * dangling_mass * inv_n
            else:
                # user-supplied redistribution (e.g. uniform over neighbors)
                dangling_mass = sum(scores[n] for n in dangling_nodes)
                total = sum(dangling.values()) or 1.0
                for tgt, w in dangling.items():
                    if tgt in scores_next:
                        scores_next[tgt] += alpha * dangling_mass * (w / total)

        # 2) edge-following mass
        for src, dsts in graph.adjacency.items():
            ow = out_weight[src]
            if ow <= 0:
                continue
            src_score = scores[src]
            if src_score == 0.0:
                continue
            share = src_score / ow
            for dst, w in dsts.items():
                scores_next[dst] += alpha * w * share

        # 3) teleport
        for n in nodes:
            scores_next[n] += (1.0 - alpha) * personalization[n]

        # 4) convergence check
        diff = sum(abs(scores_next[n] - scores[n]) for n in nodes)
        scores, scores_next = scores_next, scores
        # zero out the swap buffer for next iteration
        for n in nodes:
            scores_next[n] = 0.0
        if diff < tol:
            break

    # clip tiny noise to 0.0 (helps determinism in tests)
    return {n: 0.0 if math.isclose(s, 0.0, abs_tol=1e-12) else s for n, s in scores.items()}


def top_k_ppr(
    graph: GraphIndex,
    seeds: Sequence[str],
    k: int = 10,
    exclude_seeds: bool = True,
    **kwargs,
) -> List[Tuple[str, float]]:
    """Convenience wrapper: PPR + return the top-k (node_id, score) sorted desc.

    Args:
        graph: The adjacency view.
        seeds: Anchor node ids.
        k: Number of top nodes to return.
        exclude_seeds: Drop seed nodes from the result (PPR always scores
            them highest; in practice we want *neighbors* to add to the
            recall pack, not echo the seed set back).
        **kwargs: Forwarded to :func:`personalized_pagerank`.
    """
    if k <= 0:
        return []
    scores = personalized_pagerank(graph, seeds, **kwargs)
    if exclude_seeds:
        seed_set = set(seeds)
        scores = {n: s for n, s in scores.items() if n not in seed_set}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:k]


def normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """L1-normalize a score dict; returns empty dict on empty input."""
    total = sum(abs(v) for v in scores.values())
    if total <= 0:
        return {k: 0.0 for k in scores}
    return {k: v / total for k, v in scores.items()}


__all__ = [
    "personalized_pagerank",
    "top_k_ppr",
    "normalize_scores",
]