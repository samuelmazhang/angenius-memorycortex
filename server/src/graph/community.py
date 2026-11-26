"""Community detection -- Leiden / Louvain with stdlib fallback.

Used by ``GET /graph/communities`` and (optionally) to constrain PPR to a
single community per query. The dispatch requires the implementation to
work without leidenalg / python-louvain being installed, so we ship a
std-lib label-propagation algorithm that converges fast on the typical
<10K-node cortex graph and is good enough for the "find two clusters"
property test.

External libraries (when available) win via the fallback chain:
    leidenalg  -> python-louvain (community) -> label propagation
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set

from .index import GraphIndex


@dataclass(frozen=True)
class Community:
    """One detected community: an id (str) and the set of member chunk ids."""

    id: str
    member_ids: Set[str] = field(default_factory=set)

    def size(self) -> int:
        return len(self.member_ids)


def available_algorithms() -> List[str]:
    """Return the list of community-detection algorithms available in this env."""
    found: List[str] = []
    try:
        import leidenalg  # noqa: F401
        found.append("leiden")
    except Exception:
        pass
    try:
        import community as _louvain_mod  # noqa: F401
        found.append("louvain")
    except Exception:
        pass
    found.append("label_propagation")  # always available (pure stdlib)
    return found


def _run_leiden(graph: GraphIndex, seed: Optional[int]) -> List[Community]:
    import leidenalg as la
    import igraph as ig

    nodes = sorted(graph.nodes())
    idx_of: Dict[str, int] = {n: i for i, n in enumerate(nodes)}
    g = ig.Graph(n=len(nodes), directed=False)
    edges: List[tuple] = []
    weights: List[float] = []
    for src, dsts in graph.adjacency.items():
        for dst, w in dsts.items():
            i, j = idx_of[src], idx_of[dst]
            if i < j:
                edges.append((i, j))
                weights.append(w)
    g.add_edges(edges)
    g.es["weight"] = weights
    partition = la.find_partition(
        g,
        la.ModularityVertexPartition,
        weights="weight",
        seed=seed,
    )
    out: List[Community] = []
    for cid, members in enumerate(partition):
        ids = {nodes[i] for i in members}
        out.append(Community(id=f"leiden-{cid}", member_ids=ids))
    return out


def _run_louvain(graph: GraphIndex, seed: Optional[int]) -> List[Community]:
    import community as _louvain

    nodes = sorted(graph.nodes())
    idx_of: Dict[str, int] = {n: i for i, n in enumerate(nodes)}
    g = __import__("networkx").Graph()
    g.add_nodes_from(range(len(nodes)))
    for src, dsts in graph.adjacency.items():
        for dst, w in dsts.items():
            i, j = idx_of[src], idx_of[dst]
            if i < j:
                g.add_edge(i, j, weight=w)
    partition_dict = _louvain.best_partition(g, random_state=seed, weight="weight")
    groups: Dict[int, Set[str]] = defaultdict(set)
    for node_idx, comm_id in partition_dict.items():
        groups[comm_id].add(nodes[node_idx])
    return [Community(id=f"louvain-{cid}", member_ids=m) for cid, m in groups.items()]


def _label_propagation(graph: GraphIndex, seed: Optional[int]) -> List[Community]:
    """Async label propagation (Raghavan et al. 2007), stdlib-only.

    Each node starts with a unique label, then iteratively adopts the
    label most common in its weighted neighborhood. Ties broken randomly.
    Convergence when no node changes label in a full sweep, or after
    ``max_iter`` rounds. Random seed for reproducibility.
    """
    rng = random.Random(seed)
    nodes = sorted(graph.nodes())
    labels: Dict[str, int] = {n: i for i, n in enumerate(nodes)}

    for _ in range(64):  # max_iter
        rng.shuffle(nodes)
        moved = False
        for n in nodes:
            nbrs = graph.neighbors(n)
            if not nbrs:
                continue
            weight_by_label: Dict[int, float] = defaultdict(float)
            for nb, w in nbrs.items():
                weight_by_label[labels[nb]] += w
            # pick max-weight label, break ties by random + label id for determinism
            top_w = max(weight_by_label.values())
            candidates = [lab for lab, w in weight_by_label.items() if w == top_w]
            chosen = rng.choice(sorted(candidates))
            if chosen != labels[n]:
                labels[n] = chosen
                moved = True
        if not moved:
            break

    groups: Dict[int, Set[str]] = defaultdict(set)
    for n, lab in labels.items():
        groups[lab].add(n)
    return [Community(id=f"lp-{cid}", member_ids=m) for cid, m in groups.items()]


def detect_communities(
    graph: GraphIndex,
    algorithm: str = "auto",
    seed: Optional[int] = 42,
) -> List[Community]:
    """Detect communities on ``graph`` using the requested algorithm.

    Args:
        graph: The adjacency view.
        algorithm: One of ``"auto"``, ``"leiden"``, ``"louvain"``,
            ``"label_propagation"``. ``"auto"`` picks the best available:
            leidenalg > python-louvain > label_propagation.
        seed: Optional int for reproducibility. Label propagation uses it
            directly; leiden / louvain pass it through.
    """
    if algorithm not in ("auto", "leiden", "louvain", "label_propagation"):
        raise ValueError(
            f"unknown algorithm {algorithm!r}; "
            "expected one of: auto, leiden, louvain, label_propagation"
        )

    if graph.is_empty():
        return []

    available = available_algorithms()
    chain: List[str] = []
    if algorithm == "auto":
        chain = [a for a in ("leiden", "louvain", "label_propagation") if a in available]
    else:
        chain = [algorithm]

    last_err: Optional[Exception] = None
    for algo in chain:
        try:
            if algo == "leiden":
                return _run_leiden(graph, seed)
            if algo == "louvain":
                return _run_louvain(graph, seed)
            if algo == "label_propagation":
                return _label_propagation(graph, seed)
        except Exception as exc:  # pragma: no cover -- only fires if a lib misbehaves
            last_err = exc
            continue
    if last_err:
        raise last_err
    return []  # pragma: no cover -- unreachable when chain is non-empty


def community_of(
    graph: GraphIndex,
    node: str,
    communities: Sequence[Community],
) -> Optional[str]:
    """Return the community id containing ``node``, or None if absent."""
    for c in communities:
        if node in c.member_ids:
            return c.id
    return None


__all__ = [
    "Community",
    "detect_communities",
    "available_algorithms",
    "community_of",
]