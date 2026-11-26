"""GraphIndex -- in-memory adjacency view over memory_relations + memory_links.

Used by the PPR engine (server/src/graph/ppr.py) and community detector
(server/src/graph/community.py). Pure stdlib: adjacency is a plain dict
mapping ``node_id -> dict[neighbor_id -> weight]``. No networkx, no scipy.

The actual DB reads happen through injected ``db_query`` callables so unit
tests can run without a live Postgres.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class GraphEdge:
    """One undirected edge between two memory chunk ids with a weight."""

    src: str
    dst: str
    weight: float = 1.0

    def nodes(self) -> Tuple[str, str]:
        return (self.src, self.dst)


@dataclass
class GraphIndex:
    """Adjacency view of the cortex memory graph.

    Edges are unweighted on insert (weight defaults to 1.0) but callers can
    override ``weight`` to express relation strength from ``memory_relations``
    or ``memory_links``. Self-loops are dropped (a chunk is not its own
    neighbor).
    """

    adjacency: Dict[str, Dict[str, float]] = field(default_factory=lambda: defaultdict(dict))

    def add_edge(self, src: str, dst: str, weight: float = 1.0) -> None:
        """Add an undirected weighted edge; ignore self-loops."""
        if src == dst:
            return
        w = float(weight or 0.0)
        if w <= 0:
            return
        # max-merge so the strongest of (src->dst, dst->src) wins
        self.adjacency[src][dst] = max(self.adjacency[src].get(dst, 0.0), w)
        self.adjacency[dst][src] = max(self.adjacency[dst].get(src, 0.0), w)

    def add_edges(self, edges: Iterable[GraphEdge]) -> None:
        for e in edges:
            self.add_edge(e.src, e.dst, e.weight)

    # --------------------------------------------------------------- accessors

    def nodes(self) -> Set[str]:
        return set(self.adjacency.keys())

    def neighbors(self, node: str) -> Dict[str, float]:
        """Return {neighbor_id: weight} for the given node (empty if isolated)."""
        return dict(self.adjacency.get(node, {}))

    def edge_count(self) -> int:
        """Return the number of directed edges (2 per undirected edge)."""
        return sum(len(v) for v in self.adjacency.values())

    def undirected_edge_count(self) -> int:
        """Return the number of unique undirected edges."""
        seen: Set[Tuple[str, str]] = set()
        for src, dsts in self.adjacency.items():
            for dst in dsts:
                seen.add((src, dst) if src < dst else (dst, src))
        return len(seen)

    def weighted_degree(self, node: str) -> float:
        """Sum of outgoing weights (== incoming because undirected)."""
        return sum(self.adjacency.get(node, {}).values())

    def is_empty(self) -> bool:
        return not self.adjacency

    # --------------------------------------------------------------- factories

    @classmethod
    def from_edges(
        cls,
        relations: Optional[Iterable[Tuple[str, str, float]]] = None,
        links: Optional[Iterable[Tuple[str, str, float]]] = None,
    ) -> "GraphIndex":
        """Build a GraphIndex from raw (src, dst, weight) tuples."""
        idx = cls()
        if relations:
            for src, dst, w in relations:
                idx.add_edge(src, dst, w)
        if links:
            for src, dst, w in links:
                idx.add_edge(src, dst, w)
        return idx

    @classmethod
    def from_db(
        cls,
        db_query: Callable[[str, tuple], List[dict]],
        chunk_ids: Optional[List[str]] = None,
        relation_min_strength: float = 0.0,
        link_min_strength: float = 0.0,
    ) -> "GraphIndex":
        """Build from Postgres via injected ``db_query`` callable.

        If ``chunk_ids`` is provided, restrict the relation / link scan to
        edges touching at least one of those ids (used at search time to
        keep the working set small). Otherwise the full graph is loaded.
        """
        idx = cls()
        if chunk_ids is not None and not chunk_ids:
            return idx
        placeholders = ",".join(["%s"] * len(chunk_ids)) if chunk_ids else None

        # --- relations ---
        rel_rows: List[dict] = []
        if chunk_ids:
            rel_rows = db_query(
                f"""
                SELECT r.memory_id_1 AS src, r.memory_id_2 AS dst, r.strength
                FROM memory_relations r
                JOIN memory_chunks c1 ON c1.id = r.memory_id_1
                JOIN memory_chunks c2 ON c2.id = r.memory_id_2
                WHERE (r.memory_id_1 IN ({placeholders})
                       OR r.memory_id_2 IN ({placeholders}))
                  AND COALESCE(r.strength, 0) >= %s
                  AND c1.archived = 0 AND c2.archived = 0
                  AND c1.is_deprecated = FALSE AND c2.is_deprecated = FALSE
                """,
                tuple(chunk_ids + chunk_ids + [relation_min_strength]),
            )
        else:
            rel_rows = db_query(
                """
                SELECT r.memory_id_1 AS src, r.memory_id_2 AS dst, r.strength
                FROM memory_relations r
                JOIN memory_chunks c1 ON c1.id = r.memory_id_1
                JOIN memory_chunks c2 ON c2.id = r.memory_id_2
                WHERE COALESCE(r.strength, 0) >= %s
                  AND c1.archived = 0 AND c2.archived = 0
                  AND c1.is_deprecated = FALSE AND c2.is_deprecated = FALSE
                """,
                (relation_min_strength,),
            )
        for r in rel_rows:
            idx.add_edge(r["src"], r["dst"], float(r.get("strength") or 0.5))

        # --- links ---
        link_rows: List[dict] = []
        if chunk_ids:
            link_rows = db_query(
                f"""
                SELECT l.source_id AS src, l.target_id AS dst, l.strength
                FROM memory_links l
                JOIN memory_chunks cs ON cs.id = l.source_id
                JOIN memory_chunks ct ON ct.id = l.target_id
                WHERE (l.source_id IN ({placeholders})
                       OR l.target_id IN ({placeholders}))
                  AND COALESCE(l.strength, 0) >= %s
                  AND cs.archived = 0 AND ct.archived = 0
                  AND cs.is_deprecated = FALSE AND ct.is_deprecated = FALSE
                """,
                tuple(chunk_ids + chunk_ids + [link_min_strength]),
            )
        else:
            link_rows = db_query(
                """
                SELECT l.source_id AS src, l.target_id AS dst, l.strength
                FROM memory_links l
                JOIN memory_chunks cs ON cs.id = l.source_id
                JOIN memory_chunks ct ON ct.id = l.target_id
                WHERE COALESCE(l.strength, 0) >= %s
                  AND cs.archived = 0 AND ct.archived = 0
                  AND cs.is_deprecated = FALSE AND ct.is_deprecated = FALSE
                """,
                (link_min_strength,),
            )
        for r in link_rows:
            idx.add_edge(r["src"], r["dst"], float(r.get("strength") or 0.5))

        return idx


def merge_indices(*indices: GraphIndex) -> GraphIndex:
    """Return a new GraphIndex containing the union of edges from all inputs."""
    out = GraphIndex()
    for idx in indices:
        for src, dsts in idx.adjacency.items():
            for dst, w in dsts.items():
                out.add_edge(src, dst, w)
    return out


__all__ = [
    "GraphIndex",
    "GraphEdge",
    "merge_indices",
]