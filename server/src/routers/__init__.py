"""Domain routers (P0 phase 3).

CIRCULAR-IMPORT SAFETY
----------------------
No domain module imports api_pg. Instead ``register_routes(app, handlers)``
receives a dict of handler callables from api_pg and passes it to each domain
module's ``build(router, H)``. This breaks the back-reference entirely:
api_pg -> routers -> (handlers dict, no api_pg import).

Two earlier approaches failed in production (2026-11-25 restart loop):
  * eager ``from src.routers import ROUTERS`` -> circular at module import
  * lazy import but domain modules did ``from ..api_pg import fn`` -> still
    circular because api_pg was mid-initialization when routers loaded it.
"""
from __future__ import annotations

import importlib

from fastapi import APIRouter

_DOMAIN_MODULES = [
    "access_stats", "admin", "audit", "bench", "cache", "capabilities",
    "chroma", "chunks", "compress", "conflict", "consolidate", "core",
    "dashboard", "endpoints", "episodes", "eval", "forgetting", "graph",
    "health", "hierarchy", "history", "links", "list", "media", "memories",
    "merge_duplicates", "metrics", "mode", "procedural", "provenance",
    "raptor", "reconsolidate", "reindex", "relations", "resources", "root",
    "rumination", "search", "stats", "storage", "taxonomy", "ttl", "working",
]


def register_routes(app, handlers: dict) -> int:
    """Build and register all domain routers. Returns routers registered.

    Import errors are not swallowed: a broken route declaration must fail
    startup loudly rather than silently dropping endpoints. A missing handler
    in ``handlers`` also raises (KeyError) so no route is silently skipped.
    """
    count = 0
    for name in _DOMAIN_MODULES:
        mod = importlib.import_module(f".{name}_routes", __name__)
        router = APIRouter()
        mod.build(router, handlers)
        app.include_router(router)
        count += 1
    return count


__all__ = ["register_routes"]
