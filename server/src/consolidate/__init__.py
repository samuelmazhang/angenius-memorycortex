# Cortex v6.5.0 consolidation pipeline.
# Phase 1 (方向 #A) -- Mem0-style extract / conflict-resolve / scheduler / eval.
# Step 1 (#A.1) lives in this package's extract.py.
# Step 2 (#A.2) lives in this package's conflict_resolve.py.
# Step 3 (#A.3) lives in this package's scheduler.py (+ state.py).
# Step 4 (#C) lives in this package's compact.py (+ summary.py).
# Phase 2 #NEW3 -- quality monitoring lives in this package's quality.py.
# Phase 2 #9   -- RAPTOR recursive summary tree lives in raptor.py
#                  (+ raptor_query.py for top-down query).

from .compact import (  # noqa: E402  -- re-export after import
    DEFAULT_IMPORTANCE_THRESHOLD,
    CompactionResult,
    compact_long_conversation,
)
from .conflict_resolve import (  # noqa: E402
    ACTION_DEDUPE,
    ACTION_KEEP,
    ACTION_SUPERSEDE,
    VALID_ACTIONS,
    ConflictAction,
    resolve_conflicts,
)
from .quality import (  # noqa: E402
    DEFAULT_LOW_QUALITY_THRESHOLD,
    FactQualityMonitor,
    analyze_facts,
)
from .raptor import (  # noqa: E402
    DEFAULT_CLUSTER_THRESHOLD,
    DEFAULT_MAX_DEPTH,
    DEFAULT_RANDOM_STATE,
    RaptorNode,
    RaptorTree,
    build_tree,
)
from .raptor_query import (  # noqa: E402
    DEFAULT_BEAM,
    DEFAULT_TOP_K,
    query_with_raptor,
)
from .scheduler import (  # noqa: E402
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_THRESHOLD,
    DEFAULT_WORKER_POLL_INTERVAL,
    ConsolidationScheduler,
)
from .state import ConsolidationState  # noqa: E402
from .summary import (  # noqa: E402
    CHARS_PER_TOKEN,
    DEFAULT_TEMPLATE,
    SummarizeFn,
    adapt_dict_summarize_fn,
    summarize_chunks,
)

__all__ = [
    # #A.2 symbols (unchanged)
    "ACTION_DEDUPE",
    "ACTION_KEEP",
    "ACTION_SUPERSEDE",
    "VALID_ACTIONS",
    "ConflictAction",
    "resolve_conflicts",
    # #A.3 symbols
        "ConsolidationScheduler",
        "ConsolidationState",
        "DEFAULT_THRESHOLD",
        "DEFAULT_POLL_INTERVAL",
        "DEFAULT_FLUSH_INTERVAL",
        "DEFAULT_WORKER_POLL_INTERVAL",
        "DEFAULT_LEASE_SECONDS",
        "DEFAULT_MAX_RETRIES",
    # #C symbols
        "CompactionResult",
        "DEFAULT_IMPORTANCE_THRESHOLD",
        "compact_long_conversation",
        "summarize_chunks",
        "adapt_dict_summarize_fn",
        "SummarizeFn",
        "CHARS_PER_TOKEN",
        "DEFAULT_TEMPLATE",
    # Phase 2 #NEW3 symbols (new in this drop)
    "DEFAULT_LOW_QUALITY_THRESHOLD",
    "FactQualityMonitor",
    "analyze_facts",
    # Phase 2 #9 symbols (new in this drop)
    "DEFAULT_CLUSTER_THRESHOLD",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_RANDOM_STATE",
    "DEFAULT_BEAM",
    "DEFAULT_TOP_K",
    "RaptorNode",
    "RaptorTree",
    "build_tree",
    "query_with_raptor",
]
