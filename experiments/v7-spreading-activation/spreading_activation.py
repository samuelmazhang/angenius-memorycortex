"""Spreading Activation algorithm (V7 PoC-1, ADR-074).

Implements Collins & Loftus (1975) spreading activation with modern tweaks:
- decay factor per hop (default 0.7)
- activation threshold for stopping expansion
- max hops limit (defensive)
- cycle-safe (max-merge prevents infinite growth)
- pure function (no DB coupling → unit-testable)

Reference: ADR-074 in docs/.

Usage:
    from spreading_activation import spread_activation

    graph = {
        "a": {"b": 0.9, "c": 0.5},
        "b": {"a": 0.9, "d": 0.7},
        ...
    }
    results = spread_activation(seeds=["a"], graph=graph)
    # [("b", 0.63), ("d", 0.441), ...]
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# Default parameters (ADR-074 §Algorithm)
DEFAULT_DECAY = 0.7
DEFAULT_THRESHOLD = 0.05
DEFAULT_MAX_HOPS = 3


def spread_activation(
    seeds: List[str],
    graph: Dict[str, Dict[str, float]],
    decay: float = DEFAULT_DECAY,
    threshold: float = DEFAULT_THRESHOLD,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> List[Tuple[str, float]]:
    """Run spreading activation from seeds over the graph.

    Args:
        seeds: starting chunk IDs (each with initial activation 1.0)
        graph: adjacency map {node_id: {neighbor_id: edge_weight}}
        decay: per-hop attenuation factor (default 0.7)
        threshold: activation below this stops expansion (default 0.05)
        max_hops: defensive upper bound on iteration depth (default 3)

    Returns:
        List of (chunk_id, activation_score) sorted by score descending.
        Seeds ARE included (initial activation = 1.0).

    Notes:
        - Pure function, no side effects.
        - max-merge per node prevents amplification via cycles.
        - Initial seeds always get activation=1.0 regardless of decay.
    """
    if not seeds:
        return []
    if decay <= 0 or decay >= 1:
        raise ValueError(f"decay must be in (0, 1), got {decay}")
    if threshold <= 0:
        raise ValueError(f"threshold must be > 0, got {threshold}")
    if max_hops < 1:
        raise ValueError(f"max_hops must be >= 1, got {max_hops}")

    # Initialize: seeds get full activation, then propagate one hop
    # (separate from propagation loop to handle seed initial activation correctly)
    activation: Dict[str, float] = {}
    # First hop: seeds → their neighbors
    for seed in seeds:
        activation[seed] = 1.0  # seeds always present
        for neighbor, edge_weight in graph.get(seed, {}).items():
            delta = 1.0 * decay * edge_weight
            if delta > activation.get(neighbor, 0.0):
                activation[neighbor] = delta

    # Subsequent hops (hop 1, 2, ... from seeds perspective = iteration 2, 3, ...)
    for hop in range(1, max_hops):
        new_activations: Dict[str, float] = {}
        for node, score in activation.items():
            # Skip non-seed nodes below threshold (seeds already expanded at iteration 0)
            if node not in seeds and score < threshold:
                continue
            neighbors = graph.get(node, {})
            for neighbor, edge_weight in neighbors.items():
                delta = score * decay * edge_weight
                if delta > new_activations.get(neighbor, 0.0):
                    new_activations[neighbor] = delta
        if not new_activations:
            break
        # Merge: keep max of existing and new
        for k, v in new_activations.items():
            if v > activation.get(k, 0.0):
                activation[k] = v

    return sorted(activation.items(), key=lambda x: -x[1])


def _bfs_recall(
    seeds: List[str],
    graph: Dict[str, Dict[str, float]],
    min_strength: float = 0.5,
    cap: Optional[int] = 6,
) -> List[str]:
    """Reference: V6 BFS (api_pg._expand_graph_neighbors_filtered logic).

    Used as baseline for PoC-1 comparison. NOT used in production — the real
    implementation is in api_pg.py. This is a pure-Python replica for fairness
    (both algorithms run in-memory over the same synthetic graph).

    Args:
        seeds: starting nodes
        graph: adjacency map
        min_strength: only follow edges with weight >= this
        cap: max returned neighbors (None = unlimited, fair comparison with SA)

    Returns:
        Up to `cap` neighbor IDs, in encounter order.
    """
    unlimited = cap is None
    cap_limit = float('inf') if unlimited else cap

    visited = set(seeds)
    result = []
    frontier = list(seeds)
    while frontier and len(result) < cap_limit:
        next_frontier = []
        for node in frontier:
            for neighbor, weight in graph.get(node, {}).items():
                if weight < min_strength:
                    continue
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                result.append(neighbor)
                next_frontier.append(neighbor)
                if not unlimited and len(result) >= cap_limit:
                    break
            if not unlimited and len(result) >= cap_limit:
                break
        frontier = next_frontier
    return result
