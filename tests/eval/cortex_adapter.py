"""v6.5.0 Phase 1 direction #4 — Cortex adapter for LOCOMO harness.

Feeds LOCOMO conversation turns into a Cortex instance (or any stand-in
that exposes the same surface) and runs each QA pair against the search
endpoint, returning the top-K retrieved chunks for the LLM-as-Judge to
score downstream.

The adapter is *transport-only*: it does not score, judge, or write to
Postgres. It depends on ``httpx`` for the live API path and falls back to
an in-process callable for unit tests so the CI 200-case sampler does
not require a running server.

Spec mapping (PRD §7, direction #4):
- Step 2 (Cortex adapter): tests/eval/cortex_adapter.py
  - feed_conversation(conv) -> ingestion result
  - run_qa(qa, top_k) -> top-K retrieved chunks
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from tests.eval.locomo_loader import (
    LocomoConversation,
    LocomoQAPair,
    LocomoTurn,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TOP_K = 10
DEFAULT_TIMEOUT = 30.0


@dataclass
class RetrievedChunk:
    """A single top-K retrieval result for a QA pair."""
    memory_id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QAResult:
    """One (qa_pair, top-K retrievals) tuple, ready for the judge."""
    qa: LocomoQAPair
    retrieved: List[RetrievedChunk]
    latency_ms: float
    error: Optional[str] = None


@dataclass
class IngestionReport:
    """Summary of a feed_conversation() call."""
    conv_id: str
    chunks_written: int
    by_speaker: Dict[str, int]
    elapsed_ms: float
    error: Optional[str] = None


# ---------------------------------------------------------------- ingestion


def feed_conversation(
    conv: LocomoConversation,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: Optional[httpx.Client] = None,
    speaker: Optional[str] = None,
    importance: int = 5,
) -> IngestionReport:
    """POST each dialogue turn to Cortex ``/chunks`` batch endpoint.

    ``speaker`` overrides the ``speaker_name`` field so LOCOMO turns map
    onto Cortex's expected session metadata. If a ``client`` is supplied
    the call is HTTP; otherwise we instantiate a one-shot client.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT)
    started = time.perf_counter()
    chunks: List[Dict[str, Any]] = []
    by_speaker: Dict[str, int] = {}
    for turn in conv.turns:
        spk = speaker or turn.speaker or "user"
        chunks.append(
            {
                "content": turn.text,
                "speaker_name": spk,
                "importance": importance,
                "metadata": {
                    "source": "locomo",
                    "conv_id": conv.conv_id,
                    "turn_id": turn.turn_id,
                    "timestamp": turn.timestamp,
                },
            }
        )
        by_speaker[spk] = by_speaker.get(spk, 0) + 1
    try:
        resp = client.post("/chunks/batch", json={"chunks": chunks})
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        written = int(data.get("written", len(chunks)))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("feed_conversation failed for %s: %s", conv.conv_id, exc)
        if own_client:
            client.close()
        return IngestionReport(
            conv_id=conv.conv_id,
            chunks_written=0,
            by_speaker=by_speaker,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=str(exc),
        )
    if own_client:
        client.close()
    return IngestionReport(
        conv_id=conv.conv_id,
        chunks_written=written,
        by_speaker=by_speaker,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


# ---------------------------------------------------------------- retrieval


def run_qa(
    qa: LocomoQAPair,
    *,
    base_url: str = DEFAULT_BASE_URL,
    top_k: int = DEFAULT_TOP_K,
    client: Optional[httpx.Client] = None,
    search_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
) -> QAResult:
    """Run one QA pair through Cortex ``/search`` and return top-K hits.

    Two transport modes:

    1. ``search_fn`` supplied (used by unit tests): the adapter invokes
       the function with ``(query, top_k)`` and parses the list of
       chunk dicts. No HTTP.
    2. Live HTTP: ``POST /search`` with the question, return results.

    Both modes are wrapped in timing + error capture so the runner can
    continue past per-case failures without aborting the whole eval.
    """
    started = time.perf_counter()
    if search_fn is not None:
        try:
            hits = search_fn(qa.question, top_k)
        except Exception as exc:  # noqa: BLE001 — defensive: keep runner alive
            return QAResult(
                qa=qa,
                retrieved=[],
                latency_ms=(time.perf_counter() - started) * 1000.0,
                error=f"search_fn: {exc!r}",
            )
        retrieved = [_to_chunk(h, idx) for idx, h in enumerate(hits)]
        return QAResult(
            qa=qa,
            retrieved=retrieved,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    own_client = client is None
    if own_client:
        client = httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT)
    try:
        resp = client.post(
            "/search",
            json={"query": qa.question, "top_k": top_k, "include_related": True},
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        if own_client:
            client.close()
        return QAResult(
            qa=qa,
            retrieved=[],
            latency_ms=(time.perf_counter() - started) * 1000.0,
            error=str(exc),
        )
    if own_client:
        client.close()
    results = payload.get("results") or payload.get("chunks") or []
    retrieved = [_to_chunk(h, idx) for idx, h in enumerate(results)]
    return QAResult(
        qa=qa,
        retrieved=retrieved,
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )


def _to_chunk(hit: Dict[str, Any], idx: int) -> RetrievedChunk:
    """Normalize a hit dict (live API or stub) into a RetrievedChunk."""
    if not isinstance(hit, dict):
        return RetrievedChunk(
            memory_id=f"unknown-{idx}",
            text=str(hit),
            score=0.0,
        )
    return RetrievedChunk(
        memory_id=str(hit.get("memory_id") or hit.get("id") or hit.get("chunk_id") or uuid.uuid4()),
        text=str(hit.get("content") or hit.get("text") or ""),
        score=float(hit.get("score") or hit.get("composite_score") or 0.0),
        metadata={k: v for k, v in hit.items() if k not in {"content", "text", "score", "memory_id", "id"}},
    )


# ---------------------------------------------------------------- batch


def run_qa_batch(
    qa_pairs: Sequence[LocomoQAPair],
    *,
    top_k: int = DEFAULT_TOP_K,
    search_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    base_url: str = DEFAULT_BASE_URL,
) -> List[QAResult]:
    """Convenience wrapper used by the runner."""
    return [run_qa(q, top_k=top_k, search_fn=search_fn, base_url=base_url) for q in qa_pairs]


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TOP_K",
    "DEFAULT_TIMEOUT",
    "IngestionReport",
    "QAResult",
    "RetrievedChunk",
    "feed_conversation",
    "run_qa",
    "run_qa_batch",
]