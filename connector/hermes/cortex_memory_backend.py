"""Memory Cortex backend — cortex v4.2.0 HTTP client used by builtin memory tool.

When cortex is the active memory provider (config ``memory.provider: memory-cortex``),
the builtin ``memory`` tool writes through this backend instead of ``$HERMES_HOME/
memories/{MEMORY,USER}.md``. The .md files are NOT written — cortex is the single
source of truth, and when cortex is unreachable the tool returns an error.

This backend is the in-core counterpart to ``memory_cortex/__init__.py`` (which
is a user-installed plugin). The two share no code at runtime — the plugin is
loaded by Hermes after the in-core tool runs. Keeping a thin in-core client
here avoids a circular import and keeps the hot path independent of plugin
discovery.

Failure semantics:
- 3 retries with exponential backoff (1s, 2s, 4s).
- After retries exhausted, raise ``CortexUnavailableError`` — caller is
  responsible for converting to a tool error response.
- 5 consecutive failures opens a circuit breaker (60s cooldown) so the LLM
  doesn't burn latency waiting on a known-down cortex.

Environment variables (all optional, with safe defaults):
- ``MEMORY_CORTEX_URL``: cortex base URL (default ``http://127.0.0.1:1880`` — the LAN address where cortex v6.5.1 runs; localhost is unreachable on this host)
- ``MEMORY_CORTEX_TIMEOUT``: per-request timeout in seconds (default 10)
- ``MEMORY_CORTEX_RETRIES``: retry count after first failure (default 3)
- ``MEMORY_CORTEX_BREAKER_FAILS``: consecutive failures before opening breaker (default 5)
- ``MEMORY_CORTEX_BREAKER_COOLDOWN``: breaker open duration in seconds (default 60)
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
import threading
import time
from typing import Any, Dict, List, Optional

import requests  # memory_cortex plugin proved this is installed in hermes venv

logger = logging.getLogger(__name__)

CORTEX_URL: str = os.getenv("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
CORTEX_TIMEOUT: float = float(os.getenv("MEMORY_CORTEX_TIMEOUT", "10"))
RETRIES: int = int(os.getenv("MEMORY_CORTEX_RETRIES", "3"))
# FIX (2026-08-01): Bumped default 5 → 20. We have 27 endpoints in _KNOWN_BROKEN_PATHS
# (cortex V5.2.7 server-side bugs that return 500 even with correct args). With
# the old default of 5, a single load_from_disk() that hits 6+ of them in
# 90-hook chain opens the breaker for 60s, blocking ALL subsequent cortex calls.
# The whitelist already prevents known-broken paths from counting as failures;
# this raise is a backstop for the residual unknown-bug surface.
BREAKER_FAILS: int = int(os.getenv("MEMORY_CORTEX_BREAKER_FAILS", "20"))
BREAKER_COOLDOWN: float = float(os.getenv("MEMORY_CORTEX_BREAKER_COOLDOWN", "60"))

# Backoff schedule (seconds) — index = attempt count (0 = first try).
_BACKOFF_S = [0.0, 1.0, 2.0, 4.0]

# target → (cortex chunk type) 映射。这是单向的 builtin → cortex 转换。
# .md 时代 memory/user 是无类型文件,cortex 时代是有类型存储,简化建模:
#   target == "memory" → type="knowledge"
#   target == "user"   → type="preference"
# 这些 type 是 cortex schema 公认 valid,不会 fallback。
_TARGET_TO_SOURCE = {"memory": "hermes_memory", "user": "hermes_user"}
_TARGET_TO_TYPE = {"memory": "knowledge", "user": "preference"}
_TYPE_TO_TARGET = {"knowledge": "memory", "preference": "user"}


class CortexUnavailableError(RuntimeError):
    """Raised when Memory Cortex is unreachable after retries.

    The built-in memory tool converts this into a ``{success: False, error: ...}``
    response so the LLM can see the failure rather than the write being silently
    dropped.
    """


class CircuitOpen(Exception):
    """Internal: circuit breaker is open, request skipped without attempt."""


class _CircuitBreaker:
    """Thread-safe rolling counter that opens after N consecutive failures."""

    def __init__(self, fail_threshold: int, cooldown: float):
        self._threshold = fail_threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until: float = 0.0

    def allow(self) -> bool:
        return time.monotonic() >= self._open_until

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown
                logger.warning(
                    "cortex circuit breaker OPEN (cooldown=%.0fs) after %d "
                    "consecutive failures",
                    self._cooldown, self._consecutive_failures)

    def record_success(self) -> None:
        with self._lock:
            if self._consecutive_failures > 0:
                logger.info("cortex circuit breaker CLOSED after recovery")
            self._consecutive_failures = 0
            self._open_until = 0.0

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "consecutive_failures": self._consecutive_failures,
                "open_until": self._open_until,
                "open": time.monotonic() < self._open_until,
            }


_BREAKER = _CircuitBreaker(BREAKER_FAILS, BREAKER_COOLDOWN)


# Endpoints that cortex V5.2.7 has buggy (return 500 even with correct args).
# Without this whitelist, calling any of them in a smart hook causes the
# circuit breaker to open after 5 consecutive failures, blocking all
# subsequent cortex calls for 60s. We whitelist them so they record as
# breaker-successes (not failures) and only true outages trip the breaker.
_KNOWN_BROKEN_PATHS = {
    "/access-stats",
    "/audit",
    "/compress/{source_chunk_id}",
    "/consolidate/facts",
    "/core",
    "/core/{user_id}",
    "/episodes/{memory_id}",
    "/hierarchy/{node_id}",
    "/hierarchy/{node_id}/tree",
    "/links/access",
    "/links/create",
    "/links/decay",
    "/links/path/{source_id}/{target_id}",
    "/links/{source_id}",
    "/media/by-type/{media_type}",
    "/memories/{memory_id}/refresh",
    "/merge-duplicates",
    "/resources/{resource_id}/accessed",
    "/taxonomy/ancestors/{child_tag}",
    "/taxonomy/relations",
    "/taxonomy/relations/{child_tag}",
    "/working/{session_id}",
    "/working/{session_id}/context",
    "/working/{session_id}/{key}",
    "/forgetting/cleanup",
    "/forgetting/stats",  # may 500 intermittently under load
    "/relations/temporal",  # POST returns 500 with correct args
}


def _is_known_broken(path: str) -> bool:
    """Return True if the path is in the cortex-broken whitelist.

    Uses bidirectional prefix matching so that:
      - normalized template "/links/{source_id}" matches any called path "/links/abc"
      - and a called path "/links/abc" matches the normalized template.
    """
    # Normalize path: replace any {xxx} with placeholder
    norm = re.sub(r"\{[^}]+\}", "{}", path).rstrip("/")
    if norm in _KNOWN_BROKEN_PATHS:
        return True
    # Direct match (raw path)
    if path.rstrip("/") in _KNOWN_BROKEN_PATHS:
        return True
    # Bidirectional prefix matching:
    for wp in _KNOWN_BROKEN_PATHS:
        wp_norm = re.sub(r"\{[^}]+\}", "{}", wp).rstrip("/")
        # A) whitelist is prefix of called (and called is longer, with id at end)
        if norm.startswith(wp_norm + "/"):
            return True
        # B) called is prefix of whitelist (called is a shorter variant)
        if wp_norm.startswith(norm + "/"):
            return True
        # C) exact match
        if norm == wp_norm:
            return True
    return False


def _cortex_enabled() -> bool:
    """Whether the cortex takeover is active.

    Disabled by the absence of the ``memory_cortex`` plugin on disk and by the
    ``memory.provider`` config key not pointing at it. The caller is expected
    to gate on this before invoking the backend; we don't double-check here.
    """
    return True


def _request(method: str, path: str, *, json_body: Optional[dict] = None,
             params: Optional[dict] = None,
             raw: bool = False) -> Any:
    """HTTP request with retry + breaker semantics.

    Raises ``CortexUnavailableError`` if all retries fail or the breaker is open.

    ``raw=True`` bypasses JSON JSON decoding — for endpoints that return
    non-JSON bodies (e.g. ``GET /metrics`` returns Prometheus text format).
    """
    if not _BREAKER.allow():
        raise CortexUnavailableError(
            "Memory Cortex circuit breaker is open — service assumed "
            "unhealthy. Retries skipped to avoid latency.")
    url = f"{CORTEX_URL.rstrip('/')}{path}"
    last_exc: Optional[Exception] = None
    for attempt in range(RETRIES + 1):
        if attempt > 0:
            delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
            if delay > 0:
                logger.debug(
                    "cortex %s %s retry %d/%d after %.1fs",
                    method, path, attempt, RETRIES, delay)
                time.sleep(delay)
        try:
            resp = requests.request(
                method, url, json=json_body, params=params,
                timeout=CORTEX_TIMEOUT)
            resp.raise_for_status()
            _BREAKER.record_success()
            if resp.status_code == 204 or not resp.content:
                return None
            if raw:
                return resp.text
            return resp.json()
        except Exception as e:
            last_exc = e
            logger.debug("cortex %s %s attempt %d failed: %s",
                         method, path, attempt + 1, e)
    if _is_known_broken(path):
        # Cortex-side bug — don't trip the breaker; mark success so the
        # next call can proceed.
        _BREAKER.record_success()
    else:
        _BREAKER.record_failure()
    raise CortexUnavailableError(
        f"Memory Cortex {method} {path} failed after {RETRIES + 1} attempts: "
        f"{last_exc}")


# =============================================================================
# Public API used by tools/memory_tool.py
# =============================================================================


def health() -> Dict[str, Any]:
    """Return cortex /health response. Never raises — returns degraded shape."""
    try:
        return _request("GET", "/health")
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


def stats() -> Dict[str, Any]:
    try:
        return _request("GET", "/stats")
    except CortexUnavailableError:
        return {}


def list_entries(target: str, *, limit: int = 500,
                include_deprecated: bool = False) -> List[Dict[str, Any]]:
    """Return all chunks of the cortex type that maps from ``target``.

    ``target`` is the abstract "memory file" name ('memory' or 'user'). Each
    returned chunk dict has at least ``id``, ``content``, ``created_at`` keys.

    FIX (2026-08-02): filter by ``source=`` (the real hermes classification),
    not ``type=`` (which is too coarse and excluded everything). Cortex's /list
    gained a ``source`` query param.

    V6.6.1 fix (2026-10-31): ``include_deprecated=False`` by default. The
    server now hides superseded/expired facts from /list by default, because
    this list is what gets rendered into the agent's frozen system prompt.
    Callers that need the full history (the duplicate check below) opt in.
    """
    if target not in _TARGET_TO_SOURCE:
        raise ValueError(f"unknown target {target!r}")
    params = {"source": _TARGET_TO_SOURCE[target], "limit": limit}
    if include_deprecated:
        params["include_deprecated"] = "true"
    body = _request("GET", "/list", params=params)
    if isinstance(body, dict):
        return body.get("chunks", []) or body.get("results", []) or []
    return body or []



def search(query: str, *, limit: int = 5,
           type_filter: Optional[str] = None,
           min_importance: int = 1,
           include_related: bool = True) -> List[Dict[str, Any]]:
    """V5.3+: 真 hybrid semantic search via cortex /search endpoint.

    - 三路 RRF 混合检索（向量 + 字面 ts_rank + tag），服务端 hybrid 默认开启
    - include_related=True（默认）：命中后沿图谱邻居扩展并做相关度门控
      （服务端 _expand_graph_neighbors_filtered），让 recall 不只返回语义相似
      chunk，还能带出与命中强关联的记忆。V5.3 前服务端无此能力。

    Returns:
        list of result chunks (each: id/content/similarity/rrf_score/composite_score/...)
    """
    body: Dict[str, Any] = {"query": query, "limit": limit, "include_related": include_related}
    if type_filter:
        body["type"] = type_filter
    if min_importance > 1:
        body["min_importance"] = min_importance
    r = _request("POST", "/search", json_body=body)
    if isinstance(r, dict):
        return r.get("results", [])
    if isinstance(r, list):
        return r
    return []

def find_existing_chunk(target: str, content: str) -> Optional[Dict[str, Any]]:
    """Return the first chunk whose content equals ``content`` exactly, or None.

    Used by add() to honor the no-duplicate rule. We avoid a separate search
    endpoint and use /list+filter which is cheap for typical sizes (≤ a few
    hundred entries per type).
    """
    needle = content.strip()
    # V6.6.1 fix (2026-10-31): include superseded/expired chunks in the
    # duplicate check. /list now hides them by default (they must not reach
    # the system prompt), but re-adding a fact that already exists -- even a
    # deprecated one -- would create a second copy of the same statement.
    for c in list_entries(target, include_deprecated=True):
        if (c.get("content") or "").strip() == needle:
            return c
    return None


def create_entry(target: str, content: str, *,
                 importance: int = 7,
                 tags: Optional[List[str]] = None,
                 source_context: Optional[str] = None) -> Dict[str, Any]:
    """Write a new chunk and return it. Raises ``CortexUnavailableError`` on failure.

    Importance 7 (default) puts the entry in long_term — we apply "user-curated
    memory is high value" as the default. Callers can override via the kwarg.

    FIX (2026-08-02): set ``source`` to the target's hermes source
    ('hermes_memory' / 'hermes_user') so future list_entries() calls can find it.
    Was previously hard-coded to 'memory_tool' which made connector writes
    invisible to itself.
    """
    if target not in _TARGET_TO_TYPE:
        raise ValueError(f"unknown target {target!r}")
    body: dict = {
        "content": content,
        "type": _TARGET_TO_TYPE[target],
        "tags": tags or [f"builtin_{target}"],
        "source": _TARGET_TO_SOURCE[target],
        "importance": importance,
    }
    if source_context is not None:
        body["source_context"] = source_context
    return _request("POST", "/chunks", json_body=body)


def update_entry(chunk_id: str, content: str) -> Dict[str, Any]:
    """Replace content of an existing chunk by id. cortex POST /chunks/{id}."""
    return _request("POST", f"/chunks/{chunk_id}",
                    json_body={"content": content})


def delete_entry(chunk_id: str) -> bool:
    """Delete a chunk by id. Returns True on success. Raises on transport failure."""
    _request("DELETE", f"/chunks/{chunk_id}")
    return True


# =============================================================================
# Session-summary write path (added 2026-08-10, used by session_summarizer.py)
# =============================================================================
# Session summaries are a hermes-side concept: every end-of-session (and every
# progressive checkpoint at PROGRESSIVE_THRESHOLD assistant messages) the agent
# auto-summarizes the conversation via 9b and pushes the summary into cortex as
# a ``session_summary`` type chunk. The downstream session's first turn pulls
# the most recent N summaries for the user_id via SessionDB.list_session_summaries
# (local SQLite cache) — this function only handles the cortex write.

_SESSION_SUMMARY_TYPE = "session_summary"  # cortex-side type for auto-extracted session summaries

# V6.1.0: connector-side mirror of server whitelist.
ALLOWED_TYPES = {
    "general", "knowledge", "preference", "skill", "fact", "project",
    "session_summary", "tool_experience", "environment-fingerprint",
    "test", "context",
}


def _validate_type(t: str) -> None:
    """V6.1.0: raise ValueError if t not in ALLOWED_TYPES."""
    if t not in ALLOWED_TYPES:
        raise ValueError(
            f"type={t!r} not allowed. Allowed types: {sorted(ALLOWED_TYPES)}"
        )


def create_session_summary(
    content: str,
    *,
    importance: int = 7,
    tags: Optional[List[str]] = None,
    session_id: str = "",
    source: str = "hermes-session-summarizer",
    extractor: str = "qwen3.5-9b",
    trigger: str = "end_session",
) -> Dict[str, Any]:
    """Write a session summary chunk to cortex.

    importance 7 (default) puts the chunk in long_term tier (180 days) — the
    right tier for "facts that should survive across sessions". Tags carry
    session_id + trigger so downstream cortex queries can group by session or
    by trigger type.

    Note: there is intentionally no per-user dimension. All agents share one
    memory namespace; recall filters by content / type / tag, not by user_id.

    Returns the created chunk dict (cortex returns ``{"id": "mem_..."}``).
    Raises ``CortexUnavailableError`` on transport failure (after retries +
    circuit breaker).
    """
    _validate_type(_SESSION_SUMMARY_TYPE)
    body: dict = {
        "content": content,
        "type": _SESSION_SUMMARY_TYPE,
        "tags": tags or ["session-summary"],
        "source": source,
        "importance": importance,
        "metadata": {
            "session_id": session_id,
            "extractor": extractor,
            "trigger": trigger,
        },
    }
    return _request("POST", "/chunks", json_body=body)


def list_session_summaries_for_user(
    user_id: str = "",
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """List recent session summaries.

    Note: ``user_id`` is preserved as a parameter for backward compatibility
    with older callers, but it is now a no-op. All agents share one memory
    namespace; recall filters by content / type / tag, not by user_id.

    Implementation note: pulls a generous batch via /list?type=session_summary
    (cortex /list?tags= is not a real tag filter, so we rely on the server-side
    type= filter instead).

    Returns list of chunk dicts ordered by created_at DESC, capped at limit.
    Returns [] if the list round-trip yields nothing.

    Cost: one /list round-trip + O(N) sort. For a typical install
    (< 1000 session_summary chunks) this is fine. If the corpus grows large,
    prefer the search() endpoint (semantic match is O(log N) via the vector
    index) for free-text recall.
    """
    # Pull a generous batch (max 500 = cortex /list hard cap).
    raw = _request(
        "GET", "/list",
        params={"type": _SESSION_SUMMARY_TYPE, "limit": min(limit * 20, 500)},
    )
    chunks: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        chunks = raw.get("chunks", []) or raw.get("results", []) or []
    elif isinstance(raw, list):
        chunks = raw
    # Exclude superseded chunks from the default recall.
    matched = [
        c for c in chunks
        if not c.get("is_deprecated", False)
    ]
    # Sort by created_at DESC (lexical works for ISO timestamps)
    matched.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return matched[:limit]


def list_session_summaries_for_user_include_superseded(
    user_id: str = "",
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Same as list_session_summaries_for_user but ALSO returns superseded chunks.

    Note: ``user_id`` is preserved as a parameter for backward compatibility
    but is now a no-op. All agents share one memory namespace.

    Useful for auditing the conflict-resolve history (e.g. "show me all
    versions of session_summary for session Y"). Default recall
    should use list_session_summaries_for_user() (the no-superseded variant).
    """
    raw = _request(
        "GET", "/list",
        params={"type": _SESSION_SUMMARY_TYPE, "limit": min(limit * 20, 500)},
    )
    chunks: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        chunks = raw.get("chunks", []) or raw.get("results", []) or []
    elif isinstance(raw, list):
        chunks = raw
    matched = list(chunks)
    matched.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return matched[:limit]


def upsert_session_conflict(
    content: str,
    *,
    source_key: str,
    importance: int = 7,
    confidence: float = 0.8,
    session_id: str = "",
    trigger: str = "end_session",
    auto_resolve: bool = True,
) -> Dict[str, Any]:
    """V6.3.1 conflict-aware upsert for session summaries.

    Wraps /conflict/upsert with session_summary-specific defaults:
    - type: always "session_summary"
    - source: always "hermes-session-summarizer"
      (server /stats/by-source partitions by this label — see cortex
       OpenAPI: "Common sources: ... hermes-session-summarizer (9b auto-extract)")
    - confidence: 0.8 (default, exceeds MIN_SUPERSEDE_CONFIDENCE=0.7)

    Note: there is intentionally no per-user dimension. ``source_key`` is the
    sole bucket key for conflict detection. Callers that previously embedded
    a user_id segment in source_key should use a session-scoped source_key
    instead (e.g. f"session:{session_id}") so the bucket is shared correctly.

    When a new chunk with the same source_key arrives:
    - New chunk supersedes old (old is marked is_deprecated=true, content
      copied to memory_history with action='superseded')
    - Old chunk's id is recorded in superseded_by for chain tracking

    Returns the /conflict/upsert response dict:
        {"chunk_id": "chunk_...", "decision": {
            "action": "noop|supersede|mark_only|error",
            "old_chunk_id": "chunk_..." | None,
            "reason": str
        }}

    Raises CortexUnavailableError on transport failure.

    IMPORTANT: requires server-side BIGINT fix in memory_history.id (commit
    d54e5bc+ in cortex). Without that fix, /conflict/upsert raises a 500
    SQL error on the history insert and the supersede action is rolled back.

    V6.3.1 FIX: previously the body dict omitted the explicit ``source`` key,
    causing the server to fall back to its default (``"user"``). That broke
    /stats/by-source partitioning — every session_summary was filed under
    "user" instead of "hermes-session-summarizer". Now we explicitly set
    source to the documented label.
    """
    if not source_key:
        raise ValueError("source_key required (conflict bucket key)")

    body: dict = {
        "content": content,
        "source_key": source_key,
        # V6.3.1: explicit source label so /stats/by-source groups correctly.
        # Must match the value the server's OpenAPI documents under
        # /stats/by-source ("hermes-session-summarizer (9b auto-extract)").
        "source": "hermes-session-summarizer",
        "type": _SESSION_SUMMARY_TYPE,
        "importance": importance,
        "confidence": confidence,
        "tags": [
            "session-summary",
            f"session:{session_id}",
            f"trigger:{trigger}",
            "auto-extracted-9b",
        ],
        "auto_resolve": auto_resolve,
        "decided_by": "auto",
    }
    return _request("POST", "/conflict/upsert", json_body=body)


def lookup_entry_by_substring(target: str, old_text: str
                                ) -> Optional[Dict[str, Any]]:
    """Return the first chunk whose content contains ``old_text``, or None.

    Mirrors the builtin memory tool's "find the entry containing old_text"
    semantics. Old tool matched the entry list in-memory; we match cortex
    chunks via substring scan on the ``content`` field.
    """
    needle = old_text.strip()
    if not needle:
        return None
    # V6.6.1 fix (2026-10-31): see find_existing_chunk -- a deprecated chunk
    # still has to be findable by substring, otherwise replace/remove cannot
    # resolve its id and the entry becomes unmanageable.
    for c in list_entries(target, include_deprecated=True):
        if needle in (c.get("content") or ""):
            return c
    return None


# =============================================================================
# Health probe used by agent_init to decide whether the takeover is active.
# =============================================================================


def is_cortex_active() -> bool:
    """True if cortex is reachable. Used by agent_init as a soft gate.

    If you want the takeover to fail-loud (refuse to register the plugin
    when cortex is down), wrap calls in try/except. If you want it to fail-
    soft (silently fall back to nothing), check this first.
    """
    if not _BREAKER.allow():
        return False
    try:
        body = _request("GET", "/health", json_body=None)
        return body is not None and body.get("status") == "healthy"
    except CortexUnavailableError:
        return False
    except Exception:
        return False


# =============================================================================
# V4.3 Extended API — G1-G8 改进真接入 (让 hermes agent 真用到新功能)
# 之前 backend 只暴露 6 endpoint, v4.3 加 22 个新 endpoint 全没用
# 现在 add 18 method 把 G1-G8 真接入
# =============================================================================


# ============== G1 Working Memory ==============

def set_working_memory(session_id: str, key: str, value: str,
                       value_type: str = 'text', ttl_seconds: int = 3600) -> Optional[Dict[str, Any]]:
    """G1: 设置 working memory key/value (per-session)."""
    try:
        return _request("POST", "/working/set", json_body={
            'session_id': session_id,
            'key': key,
            'value': value,
            'value_type': value_type,
            'ttl_seconds': ttl_seconds,
        })
    except CortexUnavailableError:
        return None


def get_all_working_memory(session_id: str) -> List[Dict[str, Any]]:
    """G1: 读 session 全 working memory."""
    try:
        r = _request("GET", f"/working/{session_id}", json_body=None)
        if isinstance(r, dict):
            return r.get('items', [])
        return []
    except CortexUnavailableError:
        return []


def build_llm_context(session_id: str) -> Dict[str, Any]:
    """G1: build LLM context (working memory + conversation)."""
    try:
        return _request("GET", f"/working/{session_id}/context", json_body=None) or {}
    except CortexUnavailableError:
        return {}


def append_conversation_turn(session_id: str, role: str, content: str) -> Optional[Dict[str, Any]]:
    """G1: append 消息到 conversation rolling window."""
    try:
        return _request("POST", f"/working/{session_id}/append", json_body={
            'role': role, 'content': content,
        })
    except CortexUnavailableError:
        return None


# ============== G2 Memory Consolidation ==============

def extract_facts(chunk_id: str, content: str) -> int:
    """G2: 从 chunk content extract facts (heuristic regex)."""
    try:
        r = _request("POST", "/consolidate/extract", json_body={
            'chunk_id': chunk_id, 'content': content,
        })
        return r.get('count', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


def apply_facts_to_core(min_confidence: float = 0.5) -> int:
    """G2: apply extracted facts to the shared core profile."""
    try:
        r = _request("POST", "/consolidate/run", json_body={
            'min_confidence': min_confidence,
        })
        return r.get('applied_count', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== G3 Zettelkasten Dynamic Links ==============

def create_link(source_id: str, target_id: str, link_type: str = 'related',
                strength: float = 0.5, source: str = 'manual') -> Optional[str]:
    """G3: create Zettelkasten link."""
    try:
        r = _request("POST", "/links/create", json_body={
            'source_id': source_id, 'target_id': target_id,
            'link_type': link_type, 'strength': strength, 'source': source,
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def access_link(source_id: str, target_id: str, link_type: str = 'related') -> int:
    """G3: 记录 retrieve trace (boost strength)."""
    try:
        r = _request("POST", "/links/access", json_body={
            'source_id': source_id, 'target_id': target_id, 'link_type': link_type,
        })
        return r.get('updated', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


def auto_link_new_chunk(chunk_id: str, content: str, *, top_k: int = 5,
                        min_similarity: float = 0.30, max_links: int = 3) -> int:
    """Direction 2 (关联挖掘): auto-build Zettelkasten links for a new chunk.

    Uses hybrid search (vector + FTS5 + PageRank) to find the most semantically
    similar existing chunks, then creates 'related' links to the top ones that
    clear the similarity bar and aren't the chunk itself. Returns how many links
    were created. Fire-and-forget: returns 0 on any error (never raises).
    """
    links_created = 0
    try:
        res = search_hybrid(content, top_k=top_k)
        results = (res or {}).get('results', []) if isinstance(res, dict) else (res or [])
        for hit in results:
            if links_created >= max_links:
                break
            if not isinstance(hit, dict):
                continue
            hid = hit.get('id')
            if not hid or hid == chunk_id:
                continue
            sim = hit.get('similarity') or hit.get('score') or 0.0
            if sim < min_similarity:
                continue
            lid = create_link(source_id=chunk_id, target_id=hid,
                              link_type='related', strength=round(min(sim, 0.95), 3),
                              source='auto')
            if lid:
                links_created += 1
    except Exception:
        return 0
    return links_created

def get_links(source_id: str, link_type: Optional[str] = None,
              min_strength: float = 0.0, limit: int = 50) -> List[Dict[str, Any]]:
    """G3: 读 source 全部 outgoing links (ranked by strength)."""
    try:
        r = _request("GET", f"/links/{source_id}", json_body={
            'link_type': link_type, 'min_strength': min_strength, 'limit': limit,
        } if False else None)  # query params, not body
        # Actually use query string
        import urllib.parse
        params = urllib.parse.urlencode({
            k: v for k, v in {
                'link_type': link_type, 'min_strength': min_strength,
                'limit': limit,
            }.items() if v is not None
        })
        r = _request("GET", f"/links/{source_id}?{params}", json_body=None)
        if isinstance(r, dict):
            return r.get('links', [])
        return []
    except CortexUnavailableError:
        return []


def auto_link_from_search(query: str, top_pairs: int = 5,
                           min_similarity: float = 0.5) -> int:
    """V6.5.1 (2026-09-10): from search, build pair-wise 'related' links client-side.

    CHANGE (server contract drift): The previous implementation POSTed to
    ``/links/auto-link`` and let the server decide pairings. That endpoint was
    REMOVED in cortex V6.5.0 — auto-linking is now the P2 scheduler's job
    (``memory_maintenance_queue`` + ``/admin/maintenance/run-once``; see ADR-258
    + cortex CHANGELOG [6.5.0 Phase 2 #NEW3b]). Calling the old path now
    returns 405 Method Not Allowed (verified 2026-09-10 against
    ``http://127.0.0.1:1880/links/auto-link``).

    New behavior — do the search, then call ``create_link`` per pair:
      - ``search(query, limit=top_pairs + 1)`` returns up to top_pairs+1 hits
      - dedupe by id (keep highest similarity per id; drops self-link risk
        if the same chunk appears twice in the hit list)
      - take consecutive-pair pairs from the deduped hit list (i < j),
        capped at top_pairs total pairs
      - skip pairs where pair_sim < min_similarity
      - call ``create_link(source_id, target_id, link_type='related',
        strength=pair_sim, source='auto_from_search')`` per pair

    Returns 0 on any error, never raises. The previous return shape
    (``{'created': N}`` from server) is replaced by an int count of links
    actually created locally. ``pair_sim=0`` (no similarity field on hits)
    is detected and surfaces as a debug log line so silent regressions
    don't go unnoticed (per D-style honest attribution, anti-pattern #5).
    """
    links_created = 0
    try:
        hits = search(query, limit=top_pairs + 1)
        # Dedupe by chunk_id (keep the highest similarity seen for that id)
        seen_sim: dict = {}
        had_sim_field = False
        for h in (hits or []):
            if not isinstance(h, dict):
                continue
            cid = h.get('id')
            if not cid:
                continue
            sim_raw = h.get('similarity')
            if sim_raw is None:
                sim_raw = h.get('score')
            if sim_raw is not None:
                had_sim_field = True
                sim = float(sim_raw)
            else:
                sim = 0.0
            prev = seen_sim.get(cid)
            if prev is None or sim > prev:
                seen_sim[cid] = sim
        chunk_ids = list(seen_sim.items())
        if len(chunk_ids) < 2:
            return 0
        if not had_sim_field:
            # No /search hit carried similarity — return 0 is correct (gate
            # trips on 0.0), but log so an operator catches the contract drift.
            logger.debug(
                "auto_link_from_search: no similarity field in %d hits; "
                "skipping pair generation to avoid silent graph staleness",
                len(chunk_ids),
            )
            return 0
        # consecutive-pair generation: (0,1), (1,2), (2,3), ...
        pairs: list = []
        for j in range(1, len(chunk_ids)):
            if len(pairs) >= top_pairs:
                break
            src_id, src_sim = chunk_ids[j - 1]
            tgt_id, tgt_sim = chunk_ids[j]
            if src_id == tgt_id:
                # Dedup happened to leave adjacent same-id rows; skip to
                # avoid a self-link. (Shouldn't happen post-dedup, but be
                # defensive in case the search result was reshaped by the
                # server.)
                continue
            pair_sim = round(min(src_sim, tgt_sim, 0.95), 3)
            if pair_sim < min_similarity:
                continue
            pairs.append((src_id, tgt_id, pair_sim))
        for src_id, tgt_id, pair_sim in pairs:
            lid = create_link(source_id=src_id, target_id=tgt_id,
                              link_type='related', strength=pair_sim,
                              source='auto_from_search')
            if lid:
                links_created += 1
    except Exception:
        return 0
    return links_created


def shortest_link_path(source_id: str, target_id: str, max_depth: int = 5) -> List[Dict[str, Any]]:
    """G3: BFS 找 source → target 最短路径."""
    try:
        r = _request("GET", f"/links/path/{source_id}/{target_id}?max_depth={max_depth}",
                     json_body=None)
        return r.get('path', []) if isinstance(r, dict) else []
    except CortexUnavailableError:
        return []


# ============== G4 MATTS + G6 RRF Hybrid ==============

def search_tts(query: str, top_k: int = 5) -> Dict[str, Any]:
    """G4 MATTS: Self-Contrast multi-strategy merge.

    FIX (2026-08-01): Cortex server expects query param name 'q' (not
    'query'). The /search/tts endpoint is FastAPI-validated and rejects
    'query=' with 422: {"loc":["query","q"],"msg":"Field required"}.
    Use 'q=' so the server actually receives the query string.
    """
    try:
        return _request("GET", f"/search/tts?q={urllib.parse.quote(query)}&top_k={top_k}",
                        json_body=None) or {"results": []}
    except CortexUnavailableError:
        return {"results": []}


def search_hybrid(query: str, top_k: int = 5) -> Dict[str, Any]:
    """G6 RRF: vector + FTS5 + PageRank 融合.

    V5.3.2 FIX: 服务端 /search/hybrid 的参数名是 `limit`（不是 `top_k`），
    旧代码传 top_k 会被 FastAPI 忽略、回退默认 limit=10。现在显式传 limit。
    """
    try:
        return _request("GET", f"/search/hybrid?q={urllib.parse.quote(query)}&limit={top_k}",
                        json_body=None) or {"results": []}
    except CortexUnavailableError:
        return {"results": []}


def graph_khop(memory_id: str, k: int = 2, min_strength: float = 0.5, limit: int = 8) -> Dict[str, Any]:
    """V5.3.0+: k-hop 子图遍历（GraphRAG 多跳推理）。

    Service: GET /graph?memory_id={id}&k={k}&min_strength={s}&limit={n}.
    From a seed chunk, BFS along memory_relations for k hops; strength threshold
    filters weak links. Returns {nodes_per_hop, total, nodes:[{id,hop,content,type,...}]}.

    Raises:
        CortexUnavailableError on service down.
    """
    try:
        return _request("GET", f"/graph/khop?memory_id={urllib.parse.quote(memory_id)}"
                               f"&k={k}&min_strength={min_strength}&limit={limit}",
                        json_body=None) or {"nodes": [], "nodes_per_hop": {}}
    except CortexUnavailableError:
        return {"nodes": [], "nodes_per_hop": {}}


def graph_mine(similarity_threshold: float = 0.72, similar_batch: int = 200,
               per_anchor: int = 3) -> Dict[str, Any]:
    """V5.3.0+: 启发式图谱回填（Similarity / shared-tag / same-source 三路挖掘）。

    PG 迁移后 memory_relations 停止生长（auto_link 只对新 chunk 建链），图谱
    稀疏到无法支撑多跳推理。graph_mine 对存量 chunk 批量挖掘关系，幂等
    （ON CONFLICT 累加强度，不重复插入）。适合做成定时任务（如每日 1 次）
    让图谱持续生长。

    Returns:
        {status, new_relations:{similarity,tag-shared,same-source}, total_new,
         total_relations_after}
    """
    try:
        return _request("POST", "/graph/mine", json_body={
            "similarity_threshold": similarity_threshold,
            "similar_batch": similar_batch,
            "per_anchor": per_anchor,
        }) or {}
    except CortexUnavailableError:
        return {"status": "unavailable"}


# ============== G5 LightMem Compression ==============

def compress_old_chunks(min_age_days: int = 30, max_importance: int = 4,
                        max_per_batch: int = 50) -> int:
    """G5: 压缩 candidate chunks."""
    try:
        r = _request("POST", "/compress/run", json_body={
            'min_age_days': min_age_days, 'max_importance': max_importance,
            'max_per_batch': max_per_batch,
        })
        return r.get('compressed_count', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== G7 Audit Log ==============

def log_audit(action: str, resource_type: str, resource_id: Optional[str] = None,
              actor: str = 'system', session_id: Optional[str] = None,
              details: Optional[Dict] = None) -> int:
    """G7: 记录 audit log entry (returns 1 on logged, 0 on failure).

    FIX (2026-08-02, black-box): the server /audit/log returns {"status":"logged"}
    (the audit row id is a SERIAL not echoed back); connector used to read
    r.get('audit_id', 0) which was always 0. Treat {"status":"logged"} as success
    → 1; the id isn't exposed by the server.
    """
    try:
        payload = {
            'action': action, 'resource_type': resource_type,
            'actor': actor, 'session_id': session_id,
        }
        if resource_id:
            payload['resource_id'] = resource_id
        if details:
            payload['details'] = details
        r = _request("POST", "/audit/log", json_body=payload)
        if isinstance(r, dict):
            # accept explicit id when present, else treat status:"logged" as ok
            aid = r.get('audit_id')
            if aid is not None:
                return int(aid)
            return 1 if r.get('status') == 'logged' else 0
        return 0
    except CortexUnavailableError:
        return 0


def query_audit(resource_type: Optional[str] = None, action: Optional[str] = None,
                 actor: Optional[str] = None, since_days: int = 7,
                 limit: int = 100) -> List[Dict[str, Any]]:
    """G7: query audit log."""
    try:
        import urllib.parse
        params = urllib.parse.urlencode({
            k: v for k, v in {
                'resource_type': resource_type, 'action': action,
                'actor': actor, 'since_days': since_days, 'limit': limit,
            }.items() if v is not None
        })
        r = _request("GET", f"/audit?{params}", json_body=None)
        if isinstance(r, dict):
            return r.get('entries', [])
        return []
    except CortexUnavailableError:
        return []


# ============== G8 TTL Auto-Archive ==============

def archive_expired_chunks() -> int:
    """G8: archive expired chunks (soft delete, mark archived=1)."""
    try:
        r = _request("POST", "/ttl/archive", json_body={})
        return r.get('archived_count', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== D1 Bi-temporal Relations (extended ADR-069) ==============

def create_temporal_relation(source_id: str, target_id: str,
                              relation_type: str = 'related',
                              weight: float = 0.5) -> Optional[str]:
    """D1: create bi-temporal relation (event + transaction time)."""
    try:
        r = _request("POST", "/relations/temporal", json_body={
            'source_id': source_id, 'target_id': target_id,
            'relation_type': relation_type, 'weight': weight,
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def get_relations_bitemporal(source_id: Optional[str] = None, *,
                              target_id: Optional[str] = None,
                              relation_type: Optional[str] = None,
                              limit: Optional[int] = None,
                              **_kwargs) -> List[Dict[str, Any]]:
    """V6.5.1 (2026-09-10): query bi-temporal relations (event_time + transaction_time).

    CHANGE (server contract drift): Server V6.5.1 removed the old 4-segment path
    ``GET /relations/temporal/{src}/{tgt}/{rel_type}/bi-temporal`` (returns 404
    Not Found; verified 2026-09-10 against ``http://127.0.0.1:1880/...``).
    The new shape is a single source-id read — ``GET /relations/temporal/{src}``
    returns ALL temporal relations for the source; client filters target_id +
    relation_type locally.

    Backward compat for existing callers (notably ``memory_tool.py:919, 936``
    pass ``memory_id=chunk_id, limit=N`` — those kwargs were silently
    ignored under the old signature because they weren't accepted):
      - ``memory_id=`` accepted as alias for ``source_id=``
      - ``target_id`` and ``relation_type`` are now OPTIONAL (server doesn't
        pre-filter, so all matches come back)
      - ``limit=`` now honored (post-fetch truncation)
    """
    src = source_id
    if src is None and 'memory_id' in _kwargs:
        src = _kwargs['memory_id']
    # F7 (code-review 2026-09-10): reject unknown kwargs instead of silently
    # dropping them — caller typos like source_od= used to be ignored, leaving
    # the operator with a silent empty result.
    bad = set(_kwargs) - {'memory_id'}
    if bad:
        raise TypeError(
            f"get_relations_bitemporal() got unexpected keyword args: {sorted(bad)}"
        )
    if not src:
        return []
    try:
        r = _request("GET", f"/relations/temporal/{src}", json_body=None)
        if isinstance(r, dict):
            results = r.get('results', r.get('relations', []))
        else:
            results = []
        if target_id is not None:
            results = [rel for rel in results if isinstance(rel, dict)
                       and rel.get('target_id') == target_id]
        if relation_type is not None:
            results = [rel for rel in results if isinstance(rel, dict)
                       and rel.get('relation_type') == relation_type]
        if limit is not None and limit >= 0:
            results = results[:limit]
        return results
    except CortexUnavailableError:
        return []


# ============== D3 Hierarchy Enrichment ==============

def enrich_chunk_with_hierarchy(chunk_id: str) -> Optional[Dict[str, Any]]:
    """D3: 给 chunk 加 hierarchy context (step → episode → session)."""
    try:
        return _request("GET", f"/hierarchy/enrich/{chunk_id}", json_body=None)
    except CortexUnavailableError:
        return None


# ============== D5 MIRIX Modules ==============

def add_media(media_type: str, ocr_text: str = '', media_url: str = '',
              parent_chunk_id: Optional[str] = None) -> Optional[str]:
    """D5: 添加 media (OCR'd content for search)."""
    try:
        r = _request("POST", "/media", json_body={
            'media_type': media_type, 'ocr_text': ocr_text,
            'media_url': media_url, 'parent_chunk_id': parent_chunk_id,
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def add_resource(resource_type: str, title: str, url: str = '',
                  description: str = '', tags: Optional[list] = None) -> Optional[str]:
    """D5: add bookmarkable resource."""
    try:
        r = _request("POST", "/resources", json_body={
            'resource_type': resource_type, 'title': title, 'url': url,
            'description': description, 'tags': tags or [],
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def increment_resource_access(resource_id: str) -> bool:
    """D5: 记录资源访问 (auto-increment accessed_count)."""
    try:
        r = _request("POST", f"/resources/{resource_id}/accessed", json_body={})
        return isinstance(r, dict) and r.get('status') == 'incremented'
    except CortexUnavailableError:
        return False


def upsert_user_profile(user_id: str, profile: Dict[str, Any],
                         display_name: str = '',
                         summary: str = '') -> Optional[str]:
    """D5 MIRIX core profile upsert."""
    try:
        r = _request("POST", f"/core/{user_id}", json_body={
            'profile': profile, 'display_name': display_name, 'summary': summary,
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """D5 MIRIX core profile read."""
    try:
        r = _request("GET", f"/core/{user_id}", json_body=None)
        if isinstance(r, dict) and 'error' not in r:
            return r
        return None
    except CortexUnavailableError:
        return None


# ============== 高层 "agent 思维" 助手 (V4.3 = 真正的主入口) ==============

def reason_with_memory(question: str, top_k: int = 5,
                       include_hierarchy: bool = True,
                       khop: int = 2) -> Dict[str, Any]:
    """RAG-style reasoning: combine search + graph k-hop + hierarchy + links.

    V5.3+: 在 search 基础上新增 **GraphRAG k-hop 子图遍历**——对 top 命中
    的记忆沿 relations 扩展 k 层（默认 2），把多跳关联节点并入上下文。
    这让"上次和 A 讨论的，和 B 相关吗"这类跨 chunk 推理有图可走，
    而不只是单点语义相似。

    Args:
        question: 自然语言查询
        top_k: semantic search 返回数
        include_hierarchy: 是否附加 hierarchy 链
        khop: 子图遍历跳数（0=关闭）

    Returns:
        {chunks, context_chunks, hierarchies, links, graph_subgraph}
    """
    chunks = search(question, limit=top_k)
    hierarchies, links = [], []
    graph_subgraph = {"enabled": khop > 0, "hops": {}, "nodes": []}
    if include_hierarchy:
        for c in chunks[:top_k]:
            cid = c.get('id')
            if cid:
                h = enrich_chunk_with_hierarchy(cid)
                if isinstance(h, dict) and h.get('hierarchy_context'):
                    hierarchies.append({'chunk_id': cid, 'context': h['hierarchy_context']})
                # 记录 link access (boost strength on retrieve, Zettelkasten)
                for l in get_links(cid, min_strength=0.3, limit=3):
                    access_link(cid, l['target_id'], l['link_type'])
    # GraphRAG k-hop：对每个 hit 的 id 做一轮子图遍历，aggregate 所有节点并去重
    if khop > 0 and chunks:
        seen_nodes = {}
        for c in chunks[:3]:
            cid = c.get('id')
            if not cid:
                continue
            try:
                sub = graph_khop(cid, k=khop, min_strength=0.5, limit=8)
            except Exception:
                sub = {"nodes": []}
            for n in sub.get("nodes", []):
                if n.get("id") and n["id"] not in seen_nodes:
                    seen_nodes[n["id"]] = n
        if seen_nodes:
            graph_subgraph["hops"] = {}
            graph_subgraph["nodes"] = list(seen_nodes.values())
        # 聚合每跳分布（近似：按各节点 hop 计数）
        hop_count = {}
        for n in graph_subgraph["nodes"]:
            h = n.get("hop", 0)
            hop_count[str(h)] = hop_count.get(str(h), 0) + 1
        graph_subgraph["hops"] = hop_count
    # 拼接注入文本：search hits + k-hop 子图节点
    ctx_lines = [f"[{i+1}] {c.get('content','')[:200]}" for i, c in enumerate(chunks)]
    if graph_subgraph.get("nodes"):
        ctx_lines.append("\n## Graph k-hop related memories:")
        for kk, n in enumerate(graph_subgraph["nodes"], start=len(chunks) + 1):
            ctx_lines.append(f"[{kk}] (hop{n.get('hop',0)}) {str(n.get('content',''))[:120]}")
    return {
        'chunks': chunks,
        'context_chunks': '\n'.join(ctx_lines),
        'hierarchies': hierarchies,
        'links': links,
        'graph_subgraph': graph_subgraph,
    }


# =============================================================================
# H1 (2026-07-27): V4.4 — 扩展 backend methods 覆盖未用的 80+ endpoints
# 让 memory_tool + SDK + MCP 真能 reach 全 103 endpoints
# =============================================================================


# ============== Hierarchy (D3) ==============

def get_hierarchy_node(node_id: str) -> Optional[Dict[str, Any]]:
    """D3: get hierarchy node by id."""
    try:
        return _request("GET", f"/hierarchy/{node_id}", json_body=None)
    except CortexUnavailableError:
        return None


def get_hierarchy_tree(root_id: str, max_depth: int = 5) -> Optional[Dict[str, Any]]:
    """D3: get hierarchy tree from root."""
    try:
        return _request("GET", f"/hierarchy/{root_id}/tree?max_depth={max_depth}", json_body=None)
    except CortexUnavailableError:
        return None


def create_hierarchy_node(level: str, title: str, parent_id: Optional[str] = None,
                          memory_chunk_id: Optional[str] = None) -> Optional[str]:
    """D3: create hierarchy node (session/episode/step).

    FIX (2026-08-02, black-box): server /hierarchy returns ``{"node_id": ...}``
    (not ``{"id": ...}``) — reading `id` silently returned None every call.
    Read ``node_id`` with an ``id`` fallback for robustness.
    """
    try:
        r = _request("POST", "/hierarchy", json_body={
            'level': level, 'title': title,
            'parent_id': parent_id, 'memory_chunk_id': memory_chunk_id,
        })
        if isinstance(r, dict):
            return r.get('node_id') or r.get('id')
        return None
    except CortexUnavailableError:
        return None


# ============== Episodes (CFGM) ==============

def create_episode(session_id: str, title: str, summary: str,
                  start_time: Optional[str] = None,
                  end_time: Optional[str] = None,
                  parent_chunk_id: Optional[str] = None) -> Optional[str]:
    """CFGM: create episode (chunk group)."""
    try:
        body = {
            'session_id': session_id, 'title': title,
            'summary': summary, 'parent_chunk_id': parent_chunk_id,
        }
        if start_time:
            body['start_time'] = start_time
        if end_time:
            body['end_time'] = end_time
        r = _request("POST", "/episodes", json_body=body)
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def get_episodes_for_memory(memory_id: str) -> List[Dict[str, Any]]:
    """CFGM: list episodes containing a memory."""
    try:
        r = _request("GET", f"/episodes/{memory_id}", json_body=None)
        if isinstance(r, dict):
            return r.get('episodes', [])
        return []
    except CortexUnavailableError:
        return []


# ============== Procedural (PlugMem) ==============

def learn_procedure(name: str, trigger: str, action: str,
                    description: str = '') -> Optional[str]:
    """PlugMem: learn a new procedure (trigger → action)."""
    try:
        r = _request("POST", "/procedural", json_body={
            'name': name, 'trigger': trigger, 'action': action,
            'description': description,
        })
        return r.get('id') if isinstance(r, dict) else None
    except CortexUnavailableError:
        return None


def search_procedures(trigger_query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """PlugMem: search procedures by trigger match."""
    try:
        r = _request("GET", f"/procedural/search?trigger={urllib.parse.quote(trigger_query)}&limit={limit}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('procedures', [])
        return []
    except CortexUnavailableError:
        return []


def record_procedure_outcome(proc_id: str, success: bool) -> bool:
    """PlugMem: record procedure outcome (boost / decay strength)."""
    try:
        r = _request("POST", f"/procedural/{proc_id}/outcome",
                     json_body={'success': success})
        return isinstance(r, dict)
    except CortexUnavailableError:
        return False


# ============== Provenance + History (data lineage) ==============

def get_provenance(memory_id: str) -> Optional[Dict[str, Any]]:
    """D1: get memory provenance (who wrote, when, what context)."""
    try:
        return _request("GET", f"/provenance/{memory_id}", json_body=None)
    except CortexUnavailableError:
        return None


def get_history(memory_id: str) -> List[Dict[str, Any]]:
    """D1: get memory history (bitemporal versions)."""
    try:
        r = _request("GET", f"/history/{memory_id}", json_body=None)
        if isinstance(r, dict):
            return r.get('history', [])
        return []
    except CortexUnavailableError:
        return []


def get_relations(memory_id: str) -> List[Dict[str, Any]]:
    """Get all relations for a memory."""
    try:
        r = _request("GET", f"/relations/{memory_id}", json_body=None)
        if isinstance(r, dict):
            return r.get('relations', [])
        return []
    except CortexUnavailableError:
        return []


# ============== Reconsolidation (mirrors CoALA + Zep) ==============

def reconsolidate(memory_id: Optional[str] = None) -> int:
    """Reconsolidate: strengthen / weaken memories based on access + age."""
    try:
        r = _request("POST", "/reconsolidate", json_body={
            'memory_id': memory_id,
        })
        return r.get('affected', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


def merge_duplicates(threshold: float = 0.85) -> int:
    """Merge near-duplicate chunks."""
    try:
        r = _request("POST", "/merge-duplicates", json_body={
            'similarity_threshold': threshold,
        })
        return r.get('merged_count', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


def refresh_memory(memory_id: str) -> bool:
    """Refresh memory (re-extract embedding + vector)."""
    try:
        r = _request("POST", f"/memories/{memory_id}/refresh", json_body={})
        return isinstance(r, dict) and r.get('status') == 'refreshed'
    except CortexUnavailableError:
        return False


# ============== Forgetting (LightMem + Ebbinghaus) ==============

def get_forgetting_stats() -> Optional[Dict[str, Any]]:
    """Get forgetting curve stats (forgetting stats)."""
    try:
        return _request("GET", "/forgetting/stats", json_body=None)
    except CortexUnavailableError:
        return None


def run_forgetting_cleanup(min_score: float = 0.1) -> int:
    """Run forgetting cleanup (drop low-importance + old memories)."""
    try:
        r = _request("POST", "/forgetting/cleanup",
                     json_body={'min_score': min_score})
        return r.get('cleaned', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


def get_expired_memories(limit: int = 100) -> List[Dict[str, Any]]:
    """List memories already expired (TTL passed)."""
    try:
        r = _request("GET", f"/forgetting/expired?limit={limit}", json_body=None)
        if isinstance(r, dict):
            return r.get('expired', [])
        return []
    except CortexUnavailableError:
        return []


def get_expiring_soon(days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
    """List memories expiring within N days.

    Args:
        days: time horizon in days (cortex param `days`)
        limit: max number of items to return (cortex param `limit`)
            — backend caps client-side so callers can pass `limit` for
            consistency with other list-style helpers (e.g., list_entries).
    """
    try:
        # Cortex response key is "expiring_soon" (not "expiring"); previous
        # code used the wrong key and silently returned [].
        r = _request("GET", f"/forgetting/expiring-soon?days={days}&limit={limit}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('expiring_soon', r.get('expiring', []))
        return []
    except CortexUnavailableError:
        return []


# ============== Rumination (auto abstract patterns) ==============

def run_rumination(min_cluster_size: int = 3) -> Optional[Dict[str, Any]]:
    """Run rumination: cluster similar memories → abstract heuristics."""
    try:
        return _request("POST", "/rumination/run",
                        json_body={'min_cluster_size': min_cluster_size})
    except CortexUnavailableError:
        return None


def get_rumination_stats() -> Optional[Dict[str, Any]]:
    """Get rumination stats."""
    try:
        return _request("GET", "/rumination/stats", json_body=None)
    except CortexUnavailableError:
        return None


def abstract_heuristics(force: bool = False) -> int:
    """Manually trigger heuristic abstraction (G2-style)."""
    try:
        r = _request("POST", "/rumination/abstract-heuristics",
                     json_body={'force': force})
        return r.get('abstracted', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== Storage management ==============

def get_storage_health() -> Optional[Dict[str, Any]]:
    """Get storage health (chroma + sqlite size + vector count)."""
    try:
        return _request("GET", "/storage/health", json_body=None)
    except CortexUnavailableError:
        return None


def enforce_storage_limits(max_size_mb: int = 1024) -> int:
    """Enforce storage limits (evict oldest if over max_size_mb)."""
    try:
        r = _request("POST", "/storage/enforce",
                     json_body={'max_size_mb': max_size_mb})
        return r.get('evicted', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== Taxonomy (hierarchical tags) ==============

def get_taxonomy_relations() -> List[Dict[str, Any]]:
    """Get taxonomy relations (tag hierarchy: parent → child)."""
    try:
        r = _request("GET", "/taxonomy/relations", json_body=None)
        if isinstance(r, dict):
            return r.get('relations', [])
        return []
    except CortexUnavailableError:
        return []


def get_taxonomy_ancestors(child_tag: str) -> List[str]:
    """Get ancestor tags in taxonomy."""
    try:
        r = _request("GET", f"/taxonomy/ancestors/{urllib.parse.quote(child_tag)}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('ancestors', [])
        return []
    except CortexUnavailableError:
        return []


def delete_taxonomy_relation(child_tag: str) -> bool:
    """Delete a taxonomy relation (parent → child)."""
    try:
        r = _request("DELETE", f"/taxonomy/relations/{urllib.parse.quote(child_tag)}",
                     json_body=None)
        return isinstance(r, dict)
    except CortexUnavailableError:
        return False


# ============== Cache (PR + chroma) ==============

def get_pr_cache_stats() -> Optional[Dict[str, Any]]:
    """Get PageRank cache stats."""
    try:
        return _request("GET", "/cache/pr/stats", json_body=None)
    except CortexUnavailableError:
        return None


def clear_pr_cache() -> bool:
    """Clear PageRank cache."""
    try:
        r = _request("POST", "/cache/pr/clear", json_body={})
        return isinstance(r, dict) and r.get('status') == 'cleared'
    except CortexUnavailableError:
        return False


def cleanup_chroma_empty_collections() -> int:
    """Drop empty chroma collections."""
    try:
        r = _request("GET", "/chroma/cleanup", json_body=None)
        return r.get('dropped', 0) if isinstance(r, dict) else 0
    except CortexUnavailableError:
        return 0


# ============== Bench (HippoRAG 2) ==============

def run_bench_multi_hop(queries: Optional[List[Dict]] = None) -> Optional[Dict[str, Any]]:
    """Run multi-hop benchmark (HippoRAG 2 style)."""
    try:
        if queries:
            return _request("POST", "/bench/multi-hop/custom",
                            json_body={'queries': queries})
        return _request("GET", "/bench/multi-hop", json_body=None)
    except CortexUnavailableError:
        return None


# ============== Eval / Feedback ==============

def submit_eval_feedback(memory_id: str, feedback_type: str,
                          score: float, comment: str = '') -> bool:
    """Submit eval feedback (RLHF for memory)."""
    try:
        r = _request("POST", "/eval/feedback", json_body={
            'memory_id': memory_id, 'feedback_type': feedback_type,
            'score': score, 'comment': comment,
        })
        return isinstance(r, dict)
    except CortexUnavailableError:
        return False


def get_access_stats(memory_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get access stats (per-memory or aggregate)."""
    try:
        if memory_id:
            return _request("GET", f"/access-stats/{memory_id}", json_body=None)
        return _request("GET", "/access-stats", json_body=None)
    except CortexUnavailableError:
        return None


# ============== Dashboard / Metrics (ops observability) ==============

def get_metrics() -> Optional[str]:
    """Get dashboard metrics.

    Tries the canonical Prometheus ``/metrics`` endpoint first; falls back to
    ``/stats`` when cortex has not implemented it (returns 404 today, so the
    fallback keeps callers functional). Returns ``None`` only when cortex is
    unreachable.

    Note: ``/metrics`` returns Prometheus text format (``text/plain``), NOT
    JSON, so we bypass :func:`_request` (which does ``resp.json()``) and use
    ``requests.get`` directly. ``/stats`` does return JSON so it goes through
    ``_request``.
    """
    try:
        resp = requests.get(
            f"{CORTEX_URL.rstrip('/')}/metrics",
            timeout=CORTEX_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (404, 501):
            # /metrics not implemented — try /stats (JSON)
            return _request("GET", "/stats", json_body=None)
        return None
    except Exception:
        return None


def get_dashboard_html() -> Optional[str]:
    """Get HTML dashboard."""
    try:
        return _request("GET", "/dashboard/html", json_body=None)
    except CortexUnavailableError:
        return None


def get_core_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """Get MIRIX core profile for user."""
    try:
        r = _request("GET", f"/core/{user_id}", json_body=None)
        return r if isinstance(r, dict) and 'error' not in r else None
    except CortexUnavailableError:
        return None


def list_core_profiles(limit: int = 50) -> List[Dict[str, Any]]:
    """List all MIRIX core profiles."""
    try:
        r = _request("GET", f"/core?limit={limit}", json_body=None)
        if isinstance(r, dict):
            return r.get('profiles', [])
        return []
    except CortexUnavailableError:
        return []


# ============== Reindex (rebuild vector index) ==============

def reindex() -> bool:
    """Rebuild vector index from sqlite chunks.

    DEAD CODE NOTE (2026-08-02): cortex server-side /reindex always returns
    {"status": "noop", "reason": "PG HNSW index is always maintained"} -- the
    PG backend has no separate vector index that can be rebuilt. This wrapper
    returns False on every call. Kept (not deleted) for backwards compat with
    any existing caller, but should not be invoked from new code.
    """
    try:
        r = _request("POST", "/reindex", json_body={})
        return isinstance(r, dict) and r.get('status') == 'reindexing'
    except CortexUnavailableError:
        return False


def reset_ops_counters() -> bool:
    """Reset ops counters."""
    try:
        r = _request("POST", "/stats/ops/reset", json_body={})
        return isinstance(r, dict) and r.get('status') == 'reset'
    except CortexUnavailableError:
        return False


def get_compressed_chunk(source_chunk_id: str) -> Optional[Dict[str, Any]]:
    """Get compressed version of a chunk (LightMem)."""
    try:
        return _request("GET", f"/compress/{source_chunk_id}", json_body=None)
    except CortexUnavailableError:
        return None


def get_consolidate_facts(fact_type: Optional[str] = None,
                          limit: int = 100) -> List[Dict[str, Any]]:
    """List extracted facts (filter by type).

    Note: there is intentionally no per-user dimension. All agents share one
    memory namespace, so ``user_id`` was removed from the signature; if
    older callers pass it positionally, it will be silently absorbed into
    ``fact_type`` (which is also str-or-None) — safe in practice because
    the server only filters on ``fact_type`` and ``limit``.
    """
    try:
        params = urllib.parse.urlencode({
            k: v for k, v in {
                'fact_type': fact_type,
                'limit': limit,
            }.items() if v is not None
        })
        r = _request("GET", f"/consolidate/facts?{params}", json_body=None)
        if isinstance(r, dict):
            return r.get('facts', [])
        return []
    except CortexUnavailableError:
        return []


def get_media_by_type(media_type: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Get media by type (OCR'd content)."""
    try:
        r = _request("GET", f"/media/by-type/{media_type}?limit={limit}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('media', [])
        return []
    except CortexUnavailableError:
        return []


def search_media(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search media by OCR text."""
    try:
        r = _request("GET", f"/media/search?q={urllib.parse.quote(query)}&limit={limit}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('results', [])
        return []
    except CortexUnavailableError:
        return []


def search_resources(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Search bookmarkable resources."""
    try:
        r = _request("GET", f"/resources/search?q={urllib.parse.quote(query)}&limit={limit}",
                     json_body=None)
        if isinstance(r, dict):
            return r.get('results', [])
        return []
    except CortexUnavailableError:
        return []


# ============== 高级 "agent 思维" 综合主入口 ==============

def deep_context_for(memory_id: str, *, include_provenance: bool = True,
                     include_history: bool = True, include_relations: bool = True,
                     include_hierarchy: bool = True, include_links: bool = True) -> Dict[str, Any]:
    """Deep context for a single memory: 综合 6 维度数据.

    这是给 agent 的 "full picture" 工具 — 一次 call 拿到:
      - chunk 本身 (call get_chunk)
      - provenance (谁写)
      - history (bi-temporal 版本)
      - relations (关联 chunk)
      - hierarchy (session/episode/step 上下文)
      - Zettelkasten links (动态 link graph)
    """
    
    result = {'memory_id': memory_id}
    # chunk
    try:
        result['chunk'] = _request("GET", f"/chunks/{memory_id}", json_body=None)
    except CortexUnavailableError:
        result['chunk'] = None
    if include_provenance:
        result['provenance'] = get_provenance(memory_id)
    if include_history:
        result['history'] = get_history(memory_id)
    if include_relations:
        result['relations'] = get_relations(memory_id)
    if include_hierarchy:
        result['hierarchy'] = enrich_chunk_with_hierarchy(memory_id)
    if include_links:
        result['links'] = get_links(memory_id, min_strength=0.0)
    return result


def session_summary(session_id: str) -> Dict[str, Any]:
    """Get a session summary: working memory + conv + recent chunks + hierarchy.

    适合 agent 在长 session 中恢复 context.
    """
    return {
        'working_memory': get_all_working_memory(session_id),
        'conversation': _request("GET", f"/working/{session_id}/context", json_body=None),
        'recent_hierarchy': [_request("GET", f"/hierarchy/enrich/{cid}", json_body=None)
                             for cid in []  # placeholder, agent 可 query by chunk_id
                             if cid][:5],
    }


def agent_health_check() -> Dict[str, Any]:
    """全栈 health check: cortex server + 关键 endpoint 状态.

    适合 agent 启动时调一次, 确认 cortex 真活着 + 关键功能 work.
    """
    results = {}
    try:
        h = health()
        results['server'] = 'alive' if h.get('status') == 'healthy' else 'down'
        results['version'] = h.get('version')
    except Exception:
        results['server'] = 'down'
    try:
        results['stats'] = stats()
    except Exception:
        pass
    try:
        results['pr_cache'] = get_pr_cache_stats()
    except Exception:
        pass
    try:
        results['storage'] = get_storage_health()
    except Exception:
        pass
    try:
        results['forgetting'] = get_forgetting_stats()
    except Exception:
        pass
    return results


def recommend_endpoints(scenario: str = 'connector_memory') -> Dict[str, Any]:
    """FIX (2026-08-02): query /endpoints/recommend/{scenario} and return what the
    cortex server says the client should use. Lets hermes' tool schema adapt to the
    live cortex capability set instead of being hardcoded.

    Returns {scenario, recommended: [paths], actions: {capability: {method, path}}}
    or an empty dict on transport/CortexUnavailableError.
    """
    try:
        return _request("GET", f"/endpoints/recommend/{scenario}", json_body=None) or {}
    except CortexUnavailableError:
        return {}
    except Exception:
        return {}


# =============================================================================
# V6.5.0 P2 write-time maintenance scheduler (ADR-258) — connector layer
# =============================================================================
# Server V6.5.0 (CHANGELOG [6.5.0 Phase 2 #NEW3b]) added memory_maintenance_queue
# (PG table) + ConsolidationScheduler (write-time enqueue, single-worker drain
# via SELECT FOR UPDATE SKIP LOCKED, 30s lease, max_retries=5). These endpoints
# let agent observe / manually intervene in that pipeline.

def get_maintenance_stats() -> Dict[str, Any]:
    """V6.5.0+: P2 scheduler health — worker_id, queue counts (pending/processing/
    done/dead), lease/flush/poll intervals, last_run_timestamp."""
    try:
        return _request("GET", "/admin/maintenance/stats", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def get_maintenance_dead(limit: int = 50) -> List[Dict[str, Any]]:
    """V6.5.0+: list poison chunks that exceeded max_retries (status=dead)."""
    try:
        r = _request("GET", f"/admin/maintenance/dead?limit={limit}", json_body=None)
        return r if isinstance(r, list) else (r.get('items', []) if isinstance(r, dict) else [])
    except CortexUnavailableError:
        return []

def enqueue_maintenance(chunk_id: str, force: bool = False) -> Dict[str, Any]:
    """V6.5.0+: manually re-queue a specific chunk_id for maintenance processing."""
    try:
        return _request("POST", f"/admin/maintenance/enqueue/{chunk_id}",
                        params={'force': 'true'} if force else None,
                        json_body=None) or {}
    except CortexUnavailableError:
        return {}

def flush_maintenance() -> Dict[str, Any]:
    """V6.5.0+: force the in-memory -> PG buffer flush (normally 200ms tick)."""
    try:
        return _request("POST", "/admin/maintenance/flush", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def run_maintenance_once() -> Dict[str, Any]:
    """V6.5.0+: manually trigger one scheduler drain cycle (SELECT FOR UPDATE
    SKIP LOCKED + resolve + mark_done). Useful for tests and on-demand runs."""
    try:
        return _request("POST", "/admin/maintenance/run-once", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def admin_deprecate_by_type(chunk_type: str, *, dry_run: bool = True,
                              limit: int = 1000) -> Dict[str, Any]:
    """V6.1.0+: batch-mark chunks of a given type as is_deprecated=true.
    Returns {deprecated_count, dry_run, sample_ids}. Connector uses this for
    governance sweeps (e.g. marking 'recovered' leftovers from early test runs).
    """
    try:
        return _request("POST", "/admin/deprecate-by-type", json_body={
            'type': chunk_type, 'dry_run': dry_run, 'limit': limit,
        }) or {}
    except CortexUnavailableError:
        return {}

def patch_chunk(chunk_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """V6.5.0+: partial update (PATCH) of a chunk — only the fields passed are
    sent. Use this instead of update_entry when you only want to bump
    importance / tags / source without re-sending the full content body."""
    if not fields:
        return None
    try:
        return _request("PATCH", f"/chunks/{chunk_id}", json_body=fields)
    except CortexUnavailableError:
        return None

def batch_create_chunks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """V6.5.0+: bulk create (one HTTP round-trip for N chunks). Each item must
    match MemoryChunkCreate schema (type, content, importance, source, tags,
    user_id). Returns [{id, action, ...}, ...]."""
    if not items:
        return []
    try:
        r = _request("POST", "/chunks/batch", json_body={'items': items})
        return r.get('items', r.get('results', [])) if isinstance(r, dict) else (r or [])
    except CortexUnavailableError:
        return []

def conflict_scan(*, source_key: Optional[str] = None,
                   type_filter: Optional[str] = None,
                   limit: int = 100) -> Dict[str, Any]:
    """V6.5.0+: scan for conflicting chunks without writing — lets the agent
    preview merge candidates before triggering conflict/upsert."""
    try:
        return _request("POST", "/conflict/scan", json_body=None, params={
            k: v for k, v in {
                'source_key': source_key, 'type_filter': type_filter, 'limit': limit,
            }.items() if v is not None
        }) or {}
    except CortexUnavailableError:
        return {}

def conflict_log(*, user_id: Optional[str] = None,
                  source_key: Optional[str] = None,
                  action: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
    """V6.5.0+: read past conflict-resolve decisions (action: 'created' /
    'superseded' / 'noop' / 'archived'). Used for auditing version chains."""
    try:
        r = _request("GET", "/conflict/log", json_body=None, params={
            k: v for k, v in {
                'user_id': user_id, 'source_key': source_key,
                'action': action, 'limit': limit,
            }.items() if v is not None
        })
        return r if isinstance(r, list) else (r.get('items', []) if isinstance(r, dict) else [])
    except CortexUnavailableError:
        return []

def link_decay(*, decay_rate: Optional[float] = None,
                source_id: Optional[str] = None) -> Dict[str, Any]:
    """V6.5.0+: time-decay the strength of all (or specific) links. Server
    applies strength *= exp(-decay_rate * age_days)."""
    try:
        return _request("POST", "/links/decay", json_body=None, params={
            k: v for k, v in {
                'decay_rate': decay_rate, 'source_id': source_id,
            }.items() if v is not None
        }) or {}
    except CortexUnavailableError:
        return {}

def run_forgetting_guided(*, force: bool = False) -> Dict[str, Any]:
    """V6.5.0+: guided forgetting pass (per-user importance threshold with
    grace rules). Connects to the same path the P2 scheduler uses for
    write-time forgetting, but lets the agent force a full sweep."""
    try:
        return _request("POST", "/forgetting/guided", json_body=None,
                        params={'force': 'true'} if force else None) or {}
    except CortexUnavailableError:
        return {}

def get_ttl_archived(*, limit: int = 100) -> List[Dict[str, Any]]:
    """V6.5.0+: read TTL-archived chunks (status='archived' — past their
    importance × age threshold). These are kept for audit but not surfaced
    in normal search."""
    try:
        r = _request("GET", "/ttl/archived", json_body=None, params={'limit': limit})
        return r if isinstance(r, list) else (r.get('items', []) if isinstance(r, dict) else [])
    except CortexUnavailableError:
        return []

def storage_evict(*, max_count: Optional[int] = None,
                   ratio: Optional[float] = None) -> Dict[str, Any]:
    """V6.5.0+: manual storage eviction (drops old cold embeddings above
    the configured retention ratio)."""
    try:
        return _request("POST", "/storage/evict", json_body=None, params={
            k: v for k, v in {
                'max_count': max_count, 'ratio': ratio,
            }.items() if v is not None
        }) or {}
    except CortexUnavailableError:
        return {}

def get_working_memory_key(session_id: str, key: str) -> Optional[Dict[str, Any]]:
    """V6.5.0+: read a single key from a session's working memory (server
    short-path for the common 'just give me one key' case)."""
    try:
        return _request("GET", f"/working/{urllib.parse.quote(session_id, safe='')}/"
                        f"{urllib.parse.quote(key, safe='')}", json_body=None)
    except CortexUnavailableError:
        return None

def delete_relations(memory_id: str) -> Dict[str, Any]:
    """V6.5.0+: delete ALL relations touching a chunk (cascade-style)."""
    try:
        return _request("DELETE", f"/relations/{urllib.parse.quote(memory_id, safe='')}",
                        json_body=None) or {}
    except CortexUnavailableError:
        return {}

def delete_specific_relation(memory_id: str, target_id: str) -> Dict[str, Any]:
    """V6.5.0+: delete one specific (memory_id -> target_id) relation."""
    try:
        return _request("DELETE",
                        f"/relations/{urllib.parse.quote(memory_id, safe='')}/"
                        f"{urllib.parse.quote(target_id, safe='')}",
                        json_body=None) or {}
    except CortexUnavailableError:
        return {}

def get_chunk_with_embedding(chunk_id: str, *, include_embedding: bool = False
                              ) -> Optional[Dict[str, Any]]:
    """V6.5.0+: explicit GET /chunks/{id} (connector previously inlined this
    via create_entry's response). The new include_embedding flag is useful
    for similarity-audit / debug flows."""
    try:
        return _request("GET", f"/chunks/{urllib.parse.quote(chunk_id, safe='')}",
                        json_body=None,
                        params={'include_embedding': 'true'} if include_embedding else None)
    except CortexUnavailableError:
        return None

def get_graph_sample(*, limit: int = 50, type_filter: Optional[str] = None,
                      category: Optional[str] = None) -> List[Dict[str, Any]]:
    """V6.5.0+: sample N chunks from the graph (random + balanced across
    type/category when filters are given). Used for ad-hoc corpus inspection."""
    try:
        r = _request("GET", "/graph/sample", json_body=None, params={
            k: v for k, v in {
                'limit': limit, 'type': type_filter, 'category': category,
            }.items() if v is not None
        })
        return r if isinstance(r, list) else (r.get('items', []) if isinstance(r, dict) else [])
    except CortexUnavailableError:
        return []

def get_relations_batch(seed_ids: List[str], *, depth: int = 2,
                          max_per_node: int = 8) -> List[Dict[str, Any]]:
    """V6.5.0+ (ADR-263): multi-hop graph walk from MULTIPLE seed ids in one
    request. Cheaper than calling graph_khop per seed when the agent needs
    context for >1 chunk (typical: recall assembly for a multi-fact question)."""
    if not seed_ids:
        return []
    try:
        r = _request("GET", "/relations/batch", json_body=None, params={
            'ids': ','.join(seed_ids),
            'depth': depth,
            'max_per_node': max_per_node,
        })
        return r.get('results', r.get('relations', [])) if isinstance(r, dict) else (r or [])
    except CortexUnavailableError:
        return []

def stats_by_source(*, source: Optional[str] = None) -> Dict[str, Any]:
    """V6.1.0+: chunk counts grouped by source tag (e.g. how many came from
    ``hermes-session-summarizer`` vs other connectors)."""
    try:
        return _request("GET", "/stats/by-source", json_body=None,
                        params={'source': source} if source else None) or {}
    except CortexUnavailableError:
        return {}

def get_mode() -> Dict[str, Any]:
    """V6.5.0+: read server mode (which scheduler phases are enabled,
    CORTEX_SCHED_ENABLED flag, etc.)."""
    try:
        return _request("GET", "/mode", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def list_endpoints() -> List[Dict[str, Any]]:
    """V6.5.0+: capability discovery — returns the full live endpoint catalog.
    Used by dynamic-schema builders to keep the client tool schema in sync
    with the server's actual API."""
    try:
        r = _request("GET", "/endpoints", json_body=None)
        return r if isinstance(r, list) else (r.get('endpoints', []) if isinstance(r, dict) else [])
    except CortexUnavailableError:
        return []

def get_metrics() -> str:
    """V6.5.1: Prometheus /metrics — 22 cortex_* metrics (scheduler state,
    queue counts, lease/flush/poll intervals, total chunks). Returns raw text
    (Prometheus exposition format) — uses _request(raw=True) to bypass JSON
    decoding which would otherwise fail on the non-JSON body."""
    try:
        r = _request("GET", "/metrics", json_body=None, raw=True)
        return r if isinstance(r, str) else ''
    except CortexUnavailableError:
        return ''


# =============================================================================
# V6.1.0 / V6.5.1 stats breakdown — connector layer
# =============================================================================

def stats_by_deprecated(*, limit: int = 50) -> Dict[str, Any]:
    """V6.1.0+: governance view — chunk counts grouped by is_deprecated."""
    try:
        return _request("GET", f"/stats/by-deprecated?limit={limit}",
                        json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_by_type() -> Dict[str, Any]:
    """V6.1.0+: chunk counts grouped by type (memory/user/skill/fact/etc.)."""
    try:
        return _request("GET", "/stats/by-type", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_by_importance() -> Dict[str, Any]:
    """V6.1.0+: chunk counts grouped by importance bucket."""
    try:
        return _request("GET", "/stats/by-importance", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_by_status() -> Dict[str, Any]:
    """V6.1.0+: chunk counts grouped by status (active/superseded/archived)."""
    try:
        return _request("GET", "/stats/by-status", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_embed_cache() -> Dict[str, Any]:
    """V6.5.1: embedding cache hit-rate + size."""
    try:
        return _request("GET", "/stats/embed-cache", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_health() -> Dict[str, Any]:
    """V6.5.1: extended health stats beyond /health."""
    try:
        return _request("GET", "/stats/health", json_body=None) or {}
    except CortexUnavailableError:
        return {}

def stats_ops() -> Dict[str, Any]:
    """V6.5.1: operational counters (per-endpoint call counts, error rates)."""
    try:
        return _request("GET", "/stats/ops", json_body=None) or {}
    except CortexUnavailableError:
        return {}
