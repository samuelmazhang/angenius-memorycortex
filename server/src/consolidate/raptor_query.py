"""Cortex v6.5.0 -- Consolidation Pipeline (Phase 2 #9): RAPTOR 顶层查询.
Purpose
-------
Given a question embedding and a pre-built :class:`RaptorTree`, traverse
the tree top-down, descending into the best-matching branch at each
level, until we reach the leaves. The returned context is the set of
leaf chunks that explain the answer, ranked by their relevance to
the question.

Design choices (per dispatch prompt):
  * ``query_embedding`` is a list[float] injected by the caller -- this
    module never reaches out to an embedding API. In tests we just
    pass a numpy array directly; production wires an embedder.
  * ``score_fn`` is injectable. The default is cosine similarity
    against L2-normalized vectors. ``None`` -> use the default.
  * Returns ``List[dict]`` where each dict carries ``chunk_id``,
    ``text``, ``score``, ``path`` (the list of node ids from root to
    the leaf). Empty list when the tree has no leaves.
  * Top-down traversal: at each internal node, rank the children by
    similarity to the question and descend into the best one only
    (``beam=1``). ``beam`` is exposed for tests that want fan-out.
  * The ranking at the leaf layer is over the **paths** themselves,
    so a chunk that lives under a higher-similarity branch is
    ranked above one under a lower-similarity branch even when the
    leaf embeddings are identical.
  * Pure: no I/O, no DB, no LLM. Deterministic for a given
    ``query_embedding`` and tree.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from .raptor import DEFAULT_RANDOM_STATE, RaptorNode, RaptorTree


logger = logging.getLogger("cortex.consolidate.raptor_query")


# ---------- Public constants ----------

DEFAULT_TOP_K: int = 10
"""Default number of leaf chunks returned by :func:`query_with_raptor`."""

DEFAULT_BEAM: int = 1
"""Default beam width -- descend into the top-1 child at each level.

NOTE (V6.6.0, 2026-10-30): a beam of 1 descends a single chain and so
returns exactly ONE result no matter what ``top_k`` says. Measured on the
LOCOMO corpus as a recall channel, beam=3 is worth +3.6 points of union
recall over beam=1 (61.6% -> 65.2%), and beam=5 is worse again (61.2%).

This module keeps beam=1 because its greedy contract is part of the
v6.5.0 public surface (the integration tests assert that a cold query
returns only cold leaves, which only holds for a greedy traversal). The
search path uses ``consolidate/raptor_index.py`` instead, which defaults
to the measured optimum of 3.
"""


# ---------- Helpers (pure functions) ----------


def _l2_normalize(vec: Sequence[float]) -> List[float]:
    """L2-normalize a 1-D vector; returns all-zeros if norm is zero."""
    if vec is None:
        return []
    arr = np.asarray(list(vec), dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0 or arr.size == 0:
        return [0.0] * arr.size
    return (arr / norm).tolist()


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two L2-normalized vectors.

    Falls back to ``0.0`` if either vector is the zero vector or the
    lengths do not match.
    """
    if a is None or b is None:
        return 0.0
    arr_a = np.asarray(a, dtype=np.float64)
    arr_b = np.asarray(b, dtype=np.float64)
    if arr_a.size == 0 or arr_b.size == 0:
        return 0.0
    if arr_a.shape != arr_b.shape:
        return 0.0
    denom = float(np.linalg.norm(arr_a) * np.linalg.norm(arr_b))
    if denom <= 0.0:
        return 0.0
    return float(np.dot(arr_a, arr_b) / denom)


def _score_node(
    node: RaptorNode,
    query_vec: List[float],
    score_fn: Optional[Callable[[Sequence[float], Sequence[float]], float]],
) -> float:
    """Compute the similarity between ``node`` and the question.

    Nodes without an embedding (e.g. a zero-vector fallback in the
    builder) get ``-inf`` so they are never picked by ``argmax``.
    """
    if node.embedding is None:
        return float("-inf")
    if score_fn is None:
        return _cosine_similarity(query_vec, node.embedding)
    try:
        return float(score_fn(query_vec, node.embedding))
    except Exception as exc:  # noqa: BLE001 -- caller fn must not crash us
        logger.warning("raptor_query score_fn raised: %s", exc)
        return float("-inf")


# ---------- Public entry ----------


def query_with_raptor(
    query_embedding: Any,
    raptor_tree: RaptorTree,
    *,
    top_k: int = DEFAULT_TOP_K,
    beam: int = DEFAULT_BEAM,
    score_fn: Optional[Callable[[Sequence[float], Sequence[float]], float]] = None,
) -> List[Dict[str, Any]]:
    """Top-down traverse the RAPTOR tree and return ranked leaf context.

    Parameters
    ----------
    query_embedding : list[float] | np.ndarray
        The question embedding. Will be L2-normalized before scoring
        (matches the convention used by :func:`build_tree`).
    raptor_tree : RaptorTree
        The tree built by :func:`raptor.build_tree`.
    top_k : int, default 10
        Maximum number of leaf results returned.
    beam : int, default 1
        Branching factor per level. ``beam=1`` is greedy (the spec's
        default); ``beam>1`` explores the top-``beam`` children at
        each level (useful for tests that want fan-out).
    score_fn : callable, optional
        ``score_fn(query, node_embedding) -> float``. ``None`` falls
        back to cosine similarity.

    Returns
    -------
    list of dict
        Each dict has::

            {
                "chunk_id":  str,         # original input chunk id (or "")
                "node_id":   str,         # leaf node id
                "text":      str,         # leaf text (original chunk content)
                "score":     float,       # cumulative relevance (sum of
                                          #   path-level scores)
                "path":      list[str],   # root -> ... -> leaf node ids
            }

        Results are sorted by ``score`` descending, ties broken by
        ``path`` lexicographic order. Empty list when the tree has no
        leaves.
    """
    if not isinstance(raptor_tree, RaptorTree):
        raise TypeError(
            "raptor_tree must be a RaptorTree, got "
            + type(raptor_tree).__name__
        )
    if top_k <= 0:
        return []
    if beam <= 0:
        beam = 1

    leaves = raptor_tree.leaf_ids
    if not leaves:
        return []

    # Normalize the query vector.
    query_vec = _l2_normalize(query_embedding)

    # If the question has no usable embedding, return the first
    # ``top_k`` leaves in input order with score 0.0 -- deterministic
    # but uninformative.
    if not query_vec or all(v == 0.0 for v in query_vec):
        out: List[Dict[str, Any]] = []
        for lid in leaves[:top_k]:
            path = raptor_tree.leaf_to_path.get(lid, [raptor_tree.root_id, lid])
            leaf = raptor_tree.nodes[lid]
            cid = leaf.chunk_ids[0] if leaf.chunk_ids else ""
            out.append(
                {
                    "chunk_id": cid,
                    "node_id": lid,
                    "text": leaf.text,
                    "score": 0.0,
                    "path": list(path),
                }
            )
        return out

    # ---- Top-down traversal: collect candidate (leaf_id, path_score, path) ----

    candidates: List[Dict[str, Any]] = []

    def _walk(node_id: str, trail: List[str], path_score: float) -> None:
        node = raptor_tree.nodes[node_id]
        score = _score_node(node, query_vec, score_fn)
        # We always append the current node's score to the path score
        # so leaves under higher-similarity branches rank above leaves
        # under lower-similarity branches even when their leaf
        # embeddings happen to be equal.
        new_trail = trail + [node_id]
        new_score = path_score + score

        if node.is_leaf:
            candidates.append(
                {
                    "leaf_id": node_id,
                    "path": new_trail,
                    "score": new_score,
                }
            )
            return

        if not node.children:
            # Defensive: a non-leaf with no children cannot happen in a
            # well-formed tree, but if it does, treat as a terminal.
            candidates.append(
                {
                    "leaf_id": node_id,
                    "path": new_trail,
                    "score": new_score,
                }
            )
            return

        # Rank children, keep top-``beam``.
        scored_children: List[tuple] = []
        for child_id in node.children:
            child = raptor_tree.nodes[child_id]
            cs = _score_node(child, query_vec, score_fn)
            scored_children.append((cs, child_id))
        scored_children.sort(key=lambda pair: (-pair[0], pair[1]))
        keep = scored_children[:beam]
        for _cs, child_id in keep:
            _walk(child_id, new_trail, new_score)

    _walk(raptor_tree.root_id, [], 0.0)

    # ---- Deduplicate by leaf_id (different paths can converge in degenerate trees) ----
    by_leaf: Dict[str, Dict[str, Any]] = {}
    for cand in candidates:
        lid = cand["leaf_id"]
        if lid not in by_leaf or cand["score"] > by_leaf[lid]["score"]:
            by_leaf[lid] = cand

    # ---- Sort: score desc, then path lex asc ----
    ordered = sorted(
        by_leaf.values(),
        key=lambda c: (-c["score"], c["path"]),
    )
    ordered = ordered[:top_k]

    # ---- Materialize result dicts ----
    results: List[Dict[str, Any]] = []
    for cand in ordered:
        leaf = raptor_tree.nodes[cand["leaf_id"]]
        cid = leaf.chunk_ids[0] if leaf.chunk_ids else ""
        results.append(
            {
                "chunk_id": cid,
                "node_id": cand["leaf_id"],
                "text": leaf.text,
                "score": cand["score"],
                "path": list(cand["path"]),
            }
        )
    return results


__all__ = [
    "DEFAULT_BEAM",
    "DEFAULT_TOP_K",
    "query_with_raptor",
]
