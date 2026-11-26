"""
Cortex v6.5.0 -- Consolidation Pipeline (#A.1): LLM extraction layer.

Purpose
-------
Given a freshly-written memory ``chunk`` plus +/-2 ``context_chunks``
(recalled neighbors), ask an OpenAI-compatible chat-completions endpoint to
pull out structured information in three buckets:

  * ``facts``        -- entity-attribute-value triples with ``confidence`` in [0,1]
  * ``timeline``     -- date -> event (ISO date strings + free-text event)
  * ``preferences``  -- user preferences, likes / dislikes / habits

The output is intentionally conservative -- if the LLM is unreachable,
returns garbage JSON, or any retry still fails, we emit the empty envelope
``{"facts": [], "timeline": [], "preferences": []}`` rather than raising.
The caller (``/consolidate/extract`` endpoint, batch scheduler, etc.) MUST
be able to swallow an empty result without crashing the request that
triggered it.

Design constraints (from dispatch prompt):
  * httpx.AsyncClient, OpenAI-compatible chat/completions shape
  * tenacity: 3 attempts, exponential backoff 30s/60s/120s (configurable)
  * temperature=0.0 (deterministic) -- important for idempotent re-runs
  * LLM unreachable / timeout -> empty dict, do NOT raise
  * JSON-schema sanity check; drop facts without confidence in [0,1]

This module deliberately accepts ``cfg`` as a duck-typed object (anything
with ``.llm_url``, ``.llm_api_key``, ``.llm_model``, ``.llm_timeout``) so
tests can inject a stub without instantiating ``CortexConfig()`` -- that
requires ``.env`` and would error in tests.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

import httpx
import tenacity


logger = logging.getLogger("cortex.consolidate.extract")


# ---------- Public constants ----------

EXTRACT_SYSTEM = (
    "You are the Cortex v6.5.0 consolidation extractor. "
    "From a chunk + context, extract structured memories and output strict JSON: "
    '{"facts": [...], "timeline": [...], "preferences": [...]}.\n'
    "Each fact must be an entity-predicate-value triple with confidence in [0,1]. "
    "Timeline is a date->event list. Preferences is a user-preferences list. "
    "Output ONLY JSON. No explanation, no markdown."
)

EXTRACT_USER_TEMPLATE = (
    "[chunk]\n{content}\n\n"
    "[context (+/-2)]\n{context}\n\n"
    "Output JSON with keys facts / timeline / preferences."
)


EMPTY_RESULT: dict = {"facts": [], "timeline": [], "preferences": []}


# ---------- Retry policy ----------

_RETRY_POLICY = dict(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=30, max=120),
    # Retry on any transport-level failure: httpx errors, built-in
    # ConnectionError / TimeoutError / OSError, or JSON/schema problems.
    # NOTE: builtin ConnectionError is NOT a subclass of httpx.HTTPError, so we
    # must list it explicitly. Tenacity uses isinstance() checks.
    retry=tenacity.retry_if_exception_type(
        (
            httpx.HTTPError,
            httpx.TimeoutException,
            ConnectionError,  # builtin (httpx.ConnectError subclasses this)
            TimeoutError,    # builtin (httpx.TimeoutException subclasses this)
            OSError,         # socket-level failures
            json.JSONDecodeError,
            ValueError,
        )
    ),
    reraise=False,  # we handle fallback ourselves; let exception bubble to except
)


# ---------- Helpers (pure functions -- fully unit-testable) ----------

def _normalize_fact(fact: Any) -> dict | None:
    """Coerce a raw fact dict into the canonical shape.

    Accepts multiple LLM idioms (subject/relation/object, head/type/tail,
    entity/predicate/value). Returns None if the fact lacks a confidence
    in [0,1] or required subject/predicate fields.
    """
    if not isinstance(fact, dict):
        return None
    entity = (
        fact.get("entity")
        or fact.get("subject")
        or fact.get("head")
        or ""
    )
    predicate = (
        fact.get("predicate")
        or fact.get("relation")
        or fact.get("type")
        or ""
    )
    if "value" in fact:
        value = fact["value"]
    elif "object" in fact:
        value = fact["object"]
    elif "tail" in fact:
        value = fact["tail"]
    else:
        value = None
    confidence = fact.get("confidence")
    if not isinstance(confidence, (int, float)):
        return None
    confidence = float(confidence)
    if confidence < 0.0 or confidence > 1.0:
        return None
    if not entity or not predicate:
        return None
    return {
        "entity": str(entity),
        "predicate": str(predicate),
        "value": value,
        "confidence": confidence,
    }


def _normalize_timeline(item: Any) -> dict | None:
    """Coerce a timeline entry into {date, event}. Returns None if empty."""
    if not isinstance(item, dict):
        return None
    date = item.get("date") or item.get("when") or ""
    event = item.get("event") or item.get("what") or ""
    if not date or not event:
        return None
    return {"date": str(date), "event": str(event)}


def _normalize_preference(item: Any) -> dict | None:
    """Coerce a preference entry into {key, value, polarity}."""
    if isinstance(item, str):
        s = item.strip()
        if not s:
            return None
        return {"key": "preference", "value": s, "polarity": "like"}
    if not isinstance(item, dict):
        return None
    key = item.get("key") or item.get("category") or "preference"
    value = item.get("value") or item.get("text") or ""
    polarity = item.get("polarity") or item.get("sentiment") or "like"
    if not value:
        return None
    if polarity not in ("like", "dislike", "neutral"):
        polarity = "like"
    return {"key": str(key), "value": str(value), "polarity": str(polarity)}


def _normalize(raw: dict) -> dict:
    """Apply per-bucket normalizers; drop invalid entries; cap sizes."""
    facts_raw = raw.get("facts") or []
    if not isinstance(facts_raw, list):
        facts_raw = []
    timeline_raw = raw.get("timeline") or []
    if not isinstance(timeline_raw, list):
        timeline_raw = []
    preferences_raw = raw.get("preferences") or []
    if not isinstance(preferences_raw, list):
        preferences_raw = []

    facts = [f for f in (_normalize_fact(x) for x in facts_raw) if f is not None]
    timeline = [t for t in (_normalize_timeline(x) for x in timeline_raw) if t is not None]
    preferences = [p for p in (_normalize_preference(x) for x in preferences_raw) if p is not None]

    MAX = 50
    facts = facts[:MAX]
    timeline = timeline[:MAX]
    preferences = preferences[:MAX]

    return {
        "facts": facts,
        "timeline": timeline,
        "preferences": preferences,
    }


def _strip_json_fence(text: str) -> str:
    """LLMs often wrap JSON in ```json ... ```; pull out the first {...} block."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


# ---------- Internal: single LLM call (with tenacity) ----------

@tenacity.retry(**_RETRY_POLICY)
async def _call_llm(
    cfg: Any,
    messages: list,
    *,
    timeout: float | None = None,
) -> dict:
    """Single attempt: POST -> parse JSON. May raise (caught by tenacity / caller)."""
    url = cfg.llm_url
    api_key = getattr(cfg, "llm_api_key", "dummy")
    model = getattr(cfg, "llm_model", "gpt-3.5-turbo")
    if timeout is None:
        timeout = float(getattr(cfg, "llm_timeout", 15))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + str(api_key)

    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": messages,
        # OpenAI-compatible: nudge the model to JSON
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)

    resp.raise_for_status()
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    cleaned = _strip_json_fence(content)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("LLM returned non-object JSON")
    return parsed


# ---------- Public entry ----------

async def extract(
    chunk: dict,
    context_chunks: Iterable[dict] | None,
    cfg: Any,
) -> dict:
    """Extract structured memories from a chunk + context.

    Args:
        chunk:           central memory chunk. Expected keys: ``id``,
                         ``content``, optionally ``type``, ``tags``,
                         ``importance``, ``metadata``. Other keys ignored.
        context_chunks:  optional iterable of neighboring chunks (recall +/-2).
        cfg:             config-like object exposing ``.llm_url``,
                         ``.llm_api_key``, ``.llm_model``, ``.llm_timeout``.
                         The real type is ``config.CortexConfig``; tests
                         inject a duck-typed stub.

    Returns:
        Dict with three keys: ``facts`` (list), ``timeline`` (list),
        ``preferences`` (list). Every fact has ``confidence`` in [0,1].

    Failure mode:
        Any exception (network, JSON parse, schema, timeout) after all
        tenacity retries is swallowed and replaced by ``EMPTY_RESULT``.
        This is the contract: the caller must NEVER see an exception
        from consolidation, even if the LLM is completely down.
    """
    content = (chunk or {}).get("content", "") or ""
    if not content.strip():
        # Nothing to extract -- short-circuit, no LLM call needed
        return dict(EMPTY_RESULT)

    # Compose context string (cap at 5 neighbors, ~200 chars each)
    ctx_lines = []
    n = 0
    for c in (context_chunks or []):
        if n >= 5:
            break
        c_content = (c or {}).get("content", "") or ""
        if c_content.strip():
            ctx_lines.append("[" + str(n + 1) + "] " + c_content[:300])
            n += 1
    context_str = "\n".join(ctx_lines) if ctx_lines else "(no context)"

    user_msg = EXTRACT_USER_TEMPLATE.format(content=content, context=context_str)
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        raw = await _call_llm(cfg, messages)
    except Exception as exc:  # noqa: BLE001 -- must swallow
        logger.warning(
            "extract LLM call exhausted retries, returning empty envelope: %s",
            exc,
        )
        return dict(EMPTY_RESULT)

    try:
        return _normalize(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract normalization failed: %s", exc)
        return dict(EMPTY_RESULT)
