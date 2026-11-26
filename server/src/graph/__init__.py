# Cortex v6.5.0 graph utilities.
# Phase 1 (方向 #B) -- GraphRAG / HippoRAG style community detection +
# personalized PageRank for `include_related=true` recall.
#
# Layout (mirrors server/src/consolidate/):
#   index.py     -- GraphIndex: adjacency from memory_relations + memory_links
#   community.py -- Leiden / Louvain / label-propagation fallback
#   ppr.py       -- personalized_pagerank (power iteration, pure stdlib)
#   cache.py     -- PPR result cache (memory_graph_ppr table)

from .community import (  # noqa: E402
    Community,
    detect_communities,
    available_algorithms,
)
from .index import GraphIndex, GraphEdge, merge_indices  # noqa: E402
from .ppr import personalized_pagerank, top_k_ppr, normalize_scores  # noqa: E402

__all__ = [
    # index
    "GraphIndex",
    "GraphEdge",
    "merge_indices",
    # community
    "Community",
    "detect_communities",
    "available_algorithms",
    # ppr
    "personalized_pagerank",
    "top_k_ppr",
    "normalize_scores",
]