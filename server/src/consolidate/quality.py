"""
Cortex v6.5.0 -- Consolidation Pipeline (Phase 2 #NEW3): fact quality monitor.

Purpose
-------
Companion to `#A.1 extract()` (LLM-extracted structured facts). The
extractor can emit facts whose ``confidence`` is below the operator's
"trust this auto-extracted fact" threshold; those need human review
before they can participate in conflict resolution or search ranking.

This module is a pure-logic analyzer: it walks a list of facts (the
shape produced by `extract.py::extract()`), classifies each one as
``low_quality`` if ``confidence < low_quality_threshold``, and returns
summary statistics + the ids that need review.

Trigger conditions (per dispatch spec)
  * The list is non-empty and contains at least one fact with a
    parseable numeric ``confidence`` in [0, 1]. Facts with a missing /
    non-numeric / out-of-range confidence are *skipped* (not counted
    in either bucket) so a malformed LLM payload cannot poison the
    ratio. This mirrors ``extract.py::_normalize_fact`` behavior.

Output contract
  * ``low_quality_count``     int   -- number of facts below threshold
  * ``avg_confidence``        float -- mean of *valid* confidences; 0.0
                                        when no valid facts supplied
  * ``low_quality_ratio``     float -- ``low_quality_count / total_valid``;
                                        0.0 when ``total_valid == 0``
  * ``needs_review_ids``      list  -- ids of low-quality facts (in
                                        input order). For facts without
                                        an explicit ``id`` field we
                                        fall back to a stable derived
                                        key (``<entity>::<predicate>``
                                        or a positional placeholder)

Design constraints (from dispatch prompt)
  * No LLM, no httpx, no DB. Pure stdlib logic.
  * Idempotent: same input list -> same output dict.
  * ``low_quality_threshold`` is configurable via the constructor and
    validated on construction (must be in [0, 1]).
  * Test boundary: ``extract.py`` / ``conflict_resolve.py`` /
    ``scheduler.py`` / ``compact.py`` are untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List


logger = logging.getLogger("cortex.consolidate.quality")


# ---------- Public constants ----------

DEFAULT_LOW_QUALITY_THRESHOLD: float = 0.5
"""Confidence values strictly below this are flagged low-quality.

The default of 0.5 follows the dispatch spec. Operators that trust
the extractor more can raise it; paranoid deployments can lower it.
"""


# ---------- Helpers (pure functions -- fully unit-testable) ----------

def _coerce_confidence(raw: Any) -> float | None:
    """Best-effort coercion of a fact ``confidence`` to a float in [0, 1].

    Returns ``None`` for anything that isn't a numeric value or falls
    outside the unit interval. ``bool`` is intentionally rejected
    because ``True``/``False`` are ``int`` subclasses in Python and
    silently passing them as confidence would distort the average.
    """
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float)):
        return None
    val = float(raw)
    if val < 0.0 or val > 1.0:
        return None
    return val


def _fact_id(fact: Dict[str, Any], fallback_index: int) -> str:
    """Resolve a stable identifier for a fact.

    Order of preference:
      1. ``fact["id"]``  -- explicit id (e.g. ``memory_extracted_facts.id``)
      2. ``<entity>::<predicate>``  -- canonical bucket key from #A.2
      3. ``f"fact#{fallback_index}"``  -- last-resort positional handle
    """
    if isinstance(fact, dict):
        raw_id = fact.get("id")
        if isinstance(raw_id, (str, int)) and str(raw_id) != "":
            return str(raw_id)
        entity = fact.get("entity")
        predicate = fact.get("predicate")
        if entity and predicate:
            return f"{entity}::{predicate}"
    return f"fact#{fallback_index}"


# ---------- Public class ----------

class FactQualityMonitor:
    """Classify LLM-extracted facts by confidence.

    Parameters
    ----------
    low_quality_threshold : float, default 0.5
        Facts with ``confidence < low_quality_threshold`` are flagged
        as low-quality. Must be in [0.0, 1.0]; ``ValueError`` raised
        otherwise.

    Attributes
    ----------
    low_quality_threshold : float
        The active threshold (rounded at construction for stability).
    """

    __slots__ = ("low_quality_threshold",)

    def __init__(self, low_quality_threshold: float = DEFAULT_LOW_QUALITY_THRESHOLD) -> None:
        if not isinstance(low_quality_threshold, (int, float)):
            raise TypeError(
                "low_quality_threshold must be a number, got "
                + type(low_quality_threshold).__name__
            )
        if isinstance(low_quality_threshold, bool):
            # bool is an int subclass; reject explicitly to avoid
            # ``True`` silently meaning ``1.0``.
            raise TypeError("low_quality_threshold must be a number, got bool")
        threshold = float(low_quality_threshold)
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(
                f"low_quality_threshold must be in [0, 1], got {threshold!r}"
            )
        self.low_quality_threshold = threshold

    # ---------- Primary API ----------

    def analyze_confidence_distribution(
        self, facts: List[Dict[str, Any]] | None,
    ) -> Dict[str, Any]:
        """Return a quality summary over the supplied facts.

        Parameters
        ----------
        facts : list of dict, optional
            Facts in the canonical ``extract.py`` shape (``entity``,
            ``predicate``, ``value``, ``confidence``, optional ``id``).
            ``None`` and ``[]`` both yield a zeroed result.

        Returns
        -------
        dict with keys:
            ``low_quality_count``   int
            ``avg_confidence``      float
            ``low_quality_ratio``   float
            ``needs_review_ids``    list[str]
        """
        empty: Dict[str, Any] = {
            "low_quality_count": 0,
            "avg_confidence": 0.0,
            "low_quality_ratio": 0.0,
            "needs_review_ids": [],
        }
        if not facts:
            return empty

        total_valid = 0
        low_count = 0
        confidence_sum = 0.0
        review_ids: List[str] = []

        for idx, fact in enumerate(facts):
            if not isinstance(fact, dict):
                # Skip non-dict entries silently -- mirrors
                # ``extract.py`` tolerance for malformed payloads.
                continue
            conf = _coerce_confidence(fact.get("confidence"))
            if conf is None:
                continue
            total_valid += 1
            confidence_sum += conf
            if conf < self.low_quality_threshold:
                low_count += 1
                review_ids.append(_fact_id(fact, idx))

        if total_valid == 0:
            # Nothing parseable: keep the zeroed shape but flag that
            # the input was unusable. avg stays 0.0, ratio stays 0.0,
            # review list is empty (no fact earned a flag).
            return empty

        return {
            "low_quality_count": low_count,
            "avg_confidence": confidence_sum / total_valid,
            "low_quality_ratio": low_count / total_valid,
            "needs_review_ids": review_ids,
        }

    # ---------- Convenience helpers ----------

    def is_low_quality(self, fact: Dict[str, Any]) -> bool:
        """Return True iff the fact's confidence is below the threshold.

        Useful for one-off checks (e.g. in a UI list-view) without
        paying for a full distribution scan.
        """
        if not isinstance(fact, dict):
            return False
        conf = _coerce_confidence(fact.get("confidence"))
        if conf is None:
            return False
        return conf < self.low_quality_threshold


# ---------- Module-level convenience ----------

def analyze_facts(
    facts: List[Dict[str, Any]] | None,
    *,
    low_quality_threshold: float = DEFAULT_LOW_QUALITY_THRESHOLD,
) -> Dict[str, Any]:
    """Functional-style wrapper around :class:`FactQualityMonitor`.

    Equivalent to ``FactQualityMonitor(low_quality_threshold).analyze_confidence_distribution(facts)``
    but spares callers the instantiation when they only need a one-shot
    summary (e.g. a CLI script or a Jupyter cell).
    """
    monitor = FactQualityMonitor(low_quality_threshold=low_quality_threshold)
    return monitor.analyze_confidence_distribution(facts)


__all__ = [
    "DEFAULT_LOW_QUALITY_THRESHOLD",
    "FactQualityMonitor",
    "analyze_facts",
]
