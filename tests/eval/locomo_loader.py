"""v6.5.0 Phase 1 direction #4 — LOCOMO 1986 case loader.

Source: snap-research/locomo, dataset file ``data/locomo10.json`` (10 convs,
1986 QA pairs total). Loader indexes conversations by ``conv_id`` so the
cortex_adapter (T-4.2) and llm_judge (T-4.3) can stream both the dialogue
turns and the QA evaluation pairs.

This module is *pure stdlib* — no LLM calls, no network, no pydantic.
It only handles parsing + indexing.

Spec mapping (PRD §7, direction #4):
- Step 1 (loader): tests/eval/locomo_loader.py — load data/locomo10.json,
  index by conv_id, expose QA + turns + speaker metadata.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

logger = logging.getLogger(__name__)

# Per-conv QA counts observed 2026-08-22 against snap-research/locomo data/locomo10.json
# (PRD §7, key correction: v2 wrote 1540 which is wrong; measured 1986).
EXPECTED_PER_CONV: List[int] = [199, 105, 193, 260, 242, 158, 190, 239, 196, 204]
EXPECTED_TOTAL: int = sum(EXPECTED_PER_CONV)  # 1986


@dataclass(frozen=True)
class LocomoQAPair:
    """One QA pair from the LOCOMO dataset."""
    conv_id: str
    qa_id: str
    question: str
    answer: str
    category: Optional[str] = None
    evidence: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocomoTurn:
    """One dialogue turn (one utterance from one speaker)."""
    conv_id: str
    turn_id: int
    speaker: str
    text: str
    timestamp: Optional[str] = None  # ISO-8601 if dataset provides it


@dataclass(frozen=True)
class LocomoConversation:
    """A single conversation: turns + QA pairs grouped together."""
    conv_id: str
    turns: List[LocomoTurn]
    qa_pairs: List[LocomoQAPair]
    sample_observation: Dict[str, Any] = field(default_factory=dict)


class LocomoDataset:
    """Indexed view over data/locomo10.json.

    Constructor parses the file once; queries use the in-memory dict.
    Sampling helpers (``iter_qa_subset``) make the 200-case CI run cheap.
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"LOCOMO file not found: {self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._conversations: Dict[str, LocomoConversation] = {}
        self._qa_index: Dict[str, LocomoQAPair] = {}
        self._build(raw)
        self._validate()

    # ------------------------------------------------------------------ build

    def _build(self, raw: Any) -> None:
        # Accept both the canonical shape ({"conversation_1": {...}, ...})
        # and a wrapped shape ({"data": [...], "version": ...}). snap-research
        # ships the canonical flat-dict form, but we defend against future
        # packaging changes.
        if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], list):
            conversations_iter: Iterable[Dict[str, Any]] = raw["data"]
        elif isinstance(raw, dict):
            conversations_iter = [v for v in raw.values() if isinstance(v, dict)]
        elif isinstance(raw, list):
            conversations_iter = raw
        else:
            raise ValueError(
                f"LOCOMO file root must be dict or list, got {type(raw).__name__}"
            )

        for conv_obj in conversations_iter:
            if not isinstance(conv_obj, dict):
                logger.warning("skipping non-dict conv entry: %r", conv_obj)
                continue
            conv_id = str(conv_obj.get("conv_id") or conv_obj.get("id") or "")
            if not conv_id:
                # Many snap-research versions name the key conv-N; fall back.
                for k, v in raw.items() if isinstance(raw, dict) else []:
                    if v is conv_obj and isinstance(k, str):
                        conv_id = k
                        break
            if not conv_id:
                logger.warning("skipping conv with no id: %r", conv_obj)
                continue

            turns = self._parse_turns(conv_id, conv_obj)
            qa_pairs = self._parse_qa(conv_id, conv_obj)
            sample_obs = conv_obj.get("sample_observation") or {}
            self._conversations[conv_id] = LocomoConversation(
                conv_id=conv_id,
                turns=turns,
                qa_pairs=qa_pairs,
                sample_observation=sample_obs,
            )
            for qa in qa_pairs:
                self._qa_index[qa.qa_id] = qa

    @staticmethod
    def _parse_turns(conv_id: str, conv_obj: Dict[str, Any]) -> List[LocomoTurn]:
        turns_raw = (
            conv_obj.get("turns")
            or conv_obj.get("conversation")
            or conv_obj.get("dialogue")
            or []
        )
        out: List[LocomoTurn] = []
        if not isinstance(turns_raw, list):
            return out
        for idx, t in enumerate(turns_raw):
            if not isinstance(t, dict):
                if isinstance(t, str):
                    out.append(LocomoTurn(conv_id=conv_id, turn_id=idx, speaker="?", text=t))
                continue
            speaker = str(t.get("speaker") or t.get("role") or t.get("from") or "?")
            text = str(t.get("text") or t.get("content") or t.get("message") or "")
            timestamp = t.get("timestamp") or t.get("ts") or t.get("date_time")
            out.append(
                LocomoTurn(
                    conv_id=conv_id,
                    turn_id=idx,
                    speaker=speaker,
                    text=text,
                    timestamp=str(timestamp) if timestamp is not None else None,
                )
            )
        return out

    @staticmethod
    def _parse_qa(conv_id: str, conv_obj: Dict[str, Any]) -> List[LocomoQAPair]:
        qa_raw = conv_obj.get("qa") or conv_obj.get("qa_pairs") or []
        out: List[LocomoQAPair] = []
        if not isinstance(qa_raw, list):
            return out
        for idx, q in enumerate(qa_raw):
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or q.get("qa_id") or f"{conv_id}-q{idx}")
            question = str(q.get("question") or q.get("q") or "")
            answer = str(q.get("answer") or q.get("a") or q.get("ground_truth") or "")
            category = q.get("category")
            evidence = q.get("evidence") or []
            if isinstance(evidence, str):
                evidence = [evidence]
            out.append(
                LocomoQAPair(
                    conv_id=conv_id,
                    qa_id=qid,
                    question=question,
                    answer=answer,
                    category=str(category) if category is not None else None,
                    evidence=[str(e) for e in evidence],
                )
            )
        return out

    def _validate(self) -> None:
        total_qa = sum(len(c.qa_pairs) for c in self._conversations.values())
        # We *do not hard-fail* on count mismatch (snap-research sometimes
        # publishes a slightly different file); we log a warning so CI stays
        # green while still surfacing drift.
        if total_qa != EXPECTED_TOTAL:
            logger.warning(
                "LOCOMO QA count drift: expected %d, got %d (10 convs loaded)",
                EXPECTED_TOTAL,
                total_qa,
            )
        if not self._conversations:
            raise ValueError("LOCOMO file parsed but zero conversations found")

    # ------------------------------------------------------------------ access

    @property
    def conversations(self) -> Dict[str, LocomoConversation]:
        return dict(self._conversations)

    @property
    def qa_index(self) -> Dict[str, LocomoQAPair]:
        return dict(self._qa_index)

    def get_conversation(self, conv_id: str) -> Optional[LocomoConversation]:
        return self._conversations.get(conv_id)

    def get_qa(self, qa_id: str) -> Optional[LocomoQAPair]:
        return self._qa_index.get(qa_id)

    def all_conversation_ids(self) -> List[str]:
        return list(self._conversations.keys())

    def total_qa(self) -> int:
        return len(self._qa_index)

    # ------------------------------------------------------------------ sample

    def iter_qa_subset(
        self,
        max_cases: Optional[int] = None,
        seed: int = 0,
        conv_ids: Optional[Iterable[str]] = None,
    ) -> Iterator[LocomoQAPair]:
        """Yield QA pairs in deterministic order, optionally truncated."""
        del seed  # deterministic ordering today; reserved for future use.
        pool: List[LocomoQAPair]
        if conv_ids is None:
            pool = list(self._qa_index.values())
        else:
            wanted = set(conv_ids)
            pool = [qa for qa in self._qa_index.values() if qa.conv_id in wanted]
        pool.sort(key=lambda q: q.qa_id)
        if max_cases is not None:
            pool = pool[:max_cases]
        yield from pool


def load_locomo(path: Union[str, Path]) -> LocomoDataset:
    """Convenience wrapper mirroring the spec's Step 1 pseudocode."""
    return LocomoDataset(path)


__all__ = [
    "EXPECTED_PER_CONV",
    "EXPECTED_TOTAL",
    "LocomoConversation",
    "LocomoDataset",
    "LocomoQAPair",
    "LocomoTurn",
    "load_locomo",
]