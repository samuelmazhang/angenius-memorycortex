"""
Cortex v6.5.0 -- Consolidation Pipeline (#A.3): scheduler state.

Lightweight, JSON-serializable state object that ``ConsolidationScheduler``
mutates as it processes chunks. The fields cover the dispatch-mandated
counters (chunks-since-last-run, total runs, action totals) plus enough
bookkeeping for forensic log lines and a state-file round-trip.

Design constraints (from dispatch prompt):
  * ``@dataclass`` with primitive types only (int / float / str).
  * ``to_dict`` / ``from_dict`` round-trip is lossless.
  * No external deps beyond stdlib.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass
class ConsolidationState:
    """Mutable, JSON-serializable state for the consolidation scheduler.

    Attributes
    ----------
    chunks_since_last_run : int
        Counter of newly-recorded chunks since the last successful run.
        Reset to 0 by ``run_once`` after actions are committed.
    last_run_at : float
        Unix epoch (seconds) of the most recent successful ``run_once``.
        ``0.0`` means "never ran".
    last_run_id : str
        Short opaque identifier of the most recent run (e.g. timestamp
        plus a hex nonce). Surfaced in logs and ``memory_conflict_log``
        audit rows.
    total_runs : int
        Lifetime counter of ``run_once`` invocations that committed at
        least one action.
    total_chunks_processed : int
        Lifetime counter of chunks that have flowed through the pipeline
        (i.e. ``extract`` produced non-empty output for them).
    total_actions : int
        Lifetime counter of ``ConflictAction`` records emitted by
        ``resolve_conflicts``.
    total_dead : int                (v6.5.0 Phase 2)
        Lifetime counter of chunks that exhausted retries and were
        flipped to ``status='dead'``.
    last_error : str                (v6.5.0 Phase 2)
        Most recent worker error (truncated to 500 chars). Empty when
        the last worker tick succeeded.
    """

    chunks_since_last_run: int = 0
    last_run_at: float = 0.0
    last_run_id: str = ""
    total_runs: int = 0
    total_chunks_processed: int = 0
    total_actions: int = 0
    total_dead: int = 0
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Lossless JSON-friendly dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ConsolidationState":
        """Inverse of :meth:`to_dict`. Missing keys fall back to defaults;
        extra keys are silently ignored (forward-compat)."""
        if not isinstance(payload, dict):
            return cls()
        out = cls()
        for f in out.__dataclass_fields__:  # type: ignore[attr-defined]
            if f in payload:
                setattr(out, f, payload[f])
        return out


__all__ = ["ConsolidationState"]