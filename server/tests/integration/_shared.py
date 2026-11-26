"""Shared fixtures + helpers for the v6.5.0 B-layer integration tests.

This module deliberately re-uses the in-memory fixtures already shipped
inside the unit-test files (``FakeDB``, ``_fact``, ``_chunk``,
``_two_clusters``) instead of re-inventing them. Each integration test
file is supposed to read like a small narrative: "given fixture X from
unit-test layer Y, run module A, hand off to module B, assert contract C".

No LLM. No httpx. No Postgres. No FastAPI app boot.
"""

from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Path wiring -- single source of truth.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(__file__)
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src"))
_TESTS = os.path.normpath(os.path.join(_HERE, ".."))

for _p in (_SRC, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Consolidate (v6.5.0 Phase 1 + Phase 2 production modules).
import consolidate  # noqa: E402
from consolidate import (  # noqa: E402
    DEFAULT_BEAM,
    DEFAULT_CLUSTER_THRESHOLD,
    DEFAULT_LOW_QUALITY_THRESHOLD,
    DEFAULT_MAX_DEPTH,
    DEFAULT_TOP_K,
    FactQualityMonitor,
    RaptorTree,
    adapt_dict_summarize_fn,
    analyze_facts,
    build_tree,
    query_with_raptor,
    resolve_conflicts,
    summarize_chunks,
)
import numpy as np  # noqa: E402

# ---------------------------------------------------------------------------
# Reuse unit-test fixtures instead of duplicating them.
# ---------------------------------------------------------------------------

# DB fixture for resolve_conflicts (ADR-068 SUPERSEDE / DEDUPE / KEEP).
from test_conflict_resolve_v6_5 import FakeDB, _fact  # noqa: E402

# RAPTOR fixtures.
from test_raptor_v6_5 import _chunk, _two_clusters  # noqa: E402


# ---------------------------------------------------------------------------
# Re-export so individual integration tests can `from _shared import ...`
# without caring which unit-test file originally defined the fixture.
# ---------------------------------------------------------------------------

__all__ = [
    # unit-test fixtures
    "FakeDB",
    "_fact",
    "_chunk",
    "_two_clusters",
    # consolidate facade
    "consolidate",
    "build_tree",
    "query_with_raptor",
    "resolve_conflicts",
    "analyze_facts",
    "summarize_chunks",
    "adapt_dict_summarize_fn",
    "FactQualityMonitor",
    "RaptorTree",
    # constants
    "DEFAULT_BEAM",
    "DEFAULT_CLUSTER_THRESHOLD",
    "DEFAULT_LOW_QUALITY_THRESHOLD",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_TOP_K",
    # third-party
    "np",
    # unittest
    "unittest",
]