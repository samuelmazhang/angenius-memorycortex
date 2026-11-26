"""PPR cache -- optional hot-path optimization backed by ``memory_graph_ppr``.

The schema is additive (no migration impact on existing tables): the
``memory_graph_ppr`` table maps ``(anchor_id, node_id) -> score`` and
carries a ``computed_at`` timestamp. A row is considered fresh if its age
is below ``ttl_seconds``; otherwise the caller reruns PPR.

Production wiring (in :func:`api_pg._expand_graph_neighbors_ppr`) should
be feature-flag gated so we can A/B against the old strength-threshold
path. This module is the cache + TTL bookkeeping; the FastAPI endpoint
itself remains in ``api_pg.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .ppr import personalized_pagerank
from .index import GraphIndex


DEFAULT_TTL_SECONDS: float = 3600.0  # 1 hour


@dataclass
class PprCacheRow:
    anchor_id: str
    node_id: str
    score: float
    computed_at: float


def _now() -> float:
    return time.time()


def load_cached(
    db_query: Callable[[str, tuple], List[dict]],
    anchor_ids: Sequence[str],
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> Dict[str, Dict[str, Tuple[float, float]]]:
    """Bulk-load cached PPR scores for the given anchor ids.

    Returns ``{anchor_id: {node_id: (score, computed_at)}}`` for rows
    within the TTL window. ``db_query`` is the injected callable
    (matches the rest of the package).
    """
    if not anchor_ids:
        return {}
    now = now if now is not None else _now()
    placeholders = ",".join(["%s"] * len(anchor_ids))
    # V6.6.0 fix (2026-10-31): computed_at is a TIMESTAMPTZ column but
    # the TTL was compared against a float epoch, so Postgres raised
    #     operator does not exist: timestamp with time zone >= numeric
    # and the whole cache path fell over. The module had never been
    # exercised (nothing called it), so the bug was invisible until the
    # search path was wired to it.
    #
    # to_timestamp() converts the epoch to a timestamptz, so the
    # comparison is now type-correct.
    rows = db_query(
        f"""
        SELECT anchor_id, node_id, score,
               EXTRACT(EPOCH FROM computed_at) AS computed_at
        FROM memory_graph_ppr
        WHERE anchor_id IN ({placeholders})
          AND computed_at >= to_timestamp(%s)
        """,
        tuple(anchor_ids) + (now - ttl_seconds,),
    )
    out: Dict[str, Dict[str, Tuple[float, float]]] = {}
    for r in rows:
        out.setdefault(r["anchor_id"], {})[r["node_id"]] = (
            float(r["score"]),
            float(r["computed_at"]),
        )
    return out


def save_cached(
    db_execute: Callable[[str, tuple], None],
    anchor_id: str,
    scores: Dict[str, float],
    now: Optional[float] = None,
) -> None:
    """Persist a single anchor's PPR scores into the cache table.

    Existing rows for the same ``anchor_id`` are deleted first so we never
    serve stale mixed-vintage results. ``db_execute`` is the injected
    callable.
    """
    if not scores:
        return
    now = now if now is not None else _now()
    db_execute(
        "DELETE FROM memory_graph_ppr WHERE anchor_id = %s",
        (anchor_id,),
    )
    # Insert in chunks of 500 to keep the parameter count bounded.
    items = list(scores.items())
    chunk = 500
    for i in range(0, len(items), chunk):
        batch = items[i:i + chunk]
        placeholders = ",".join(["(%s, %s, %s, to_timestamp(%s))"] * len(batch))
        params: list = []
        for node_id, score in batch:
            params.extend([anchor_id, node_id, float(score), now])
        db_execute(
            f"""
            INSERT INTO memory_graph_ppr
                (anchor_id, node_id, score, computed_at)
            VALUES {placeholders}
            """,
            tuple(params),
        )


def compute_or_load(
    graph: GraphIndex,
    anchor_id: str,
    db_query: Callable[[str, tuple], List[dict]],
    db_execute: Callable[[str, tuple], None],
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    seeds: Optional[Sequence[str]] = None,
    use_cache: bool = True,
) -> Dict[str, float]:
    """Return the PPR score dict for ``anchor_id``, hitting the cache when possible.

    ``seeds`` defaults to ``[anchor_id]``; callers wanting multi-seed PPR
    (e.g. the recall pipeline that unions lexical + vector hits) can pass
    a longer list and the cache will be keyed on the leading anchor only
    (caller is responsible for the dedupe).
    """
    if use_cache:
        cached = load_cached(db_query, [anchor_id], ttl_seconds=ttl_seconds)
        if cached.get(anchor_id):
            return {node_id: score for node_id, (score, _) in cached[anchor_id].items()}

    seeds_list: Sequence[str] = list(seeds) if seeds else [anchor_id]
    scores = personalized_pagerank(
        graph,
        seeds_list,
        alpha=alpha,
        max_iter=max_iter,
        tol=tol,
    )
    if use_cache:
        save_cached(db_execute, anchor_id, scores)
    return scores


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "PprCacheRow",
    "load_cached",
    "save_cached",
    "compute_or_load",
]