"""v6.5.0 Phase 1 direction #4 — LLM-as-Judge for LOCOMO QA.

Computes three offline metrics (F1, BLEU, exact match) directly from
``QAResult.retrieved`` vs ``LocomoQAPair.answer``, *plus* an optional
LLM-as-Judge rubric score using MiniMax-M3 via
``http://localhost:8080/v1/chat/completions`` (per spec §7.1 step 3).

The LLM call is opt-in (default off) because CI sandboxes may not have
network access to the local judge model. ``judge_with_llm`` returns
``None`` on any error rather than raising, so the runner can keep going.

Spec mapping (PRD §7, direction #4):
- Step 3 (LLM-as-Judge): tests/eval/llm_judge.py
  - f1_score / bleu_score / exact_match helpers (no LLM)
  - aggregate_metrics(results) -> dict with mean F1 / BLEU / EM
  - judge_with_llm(qa, top_chunks) -> optional MiniMax-M3 rubric score
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

from tests.eval.cortex_adapter import QAResult, RetrievedChunk
from tests.eval.locomo_loader import LocomoQAPair

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- tokenize


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _WORD_RE.findall(text)]


# ---------------------------------------------------------------- metrics


def exact_match(predicted: str, gold: str) -> float:
    """1.0 if normalized strings are equal, else 0.0."""
    p = " ".join(_tokenize(predicted))
    g = " ".join(_tokenize(gold))
    if not p and not g:
        return 1.0
    return 1.0 if p == g else 0.0


def f1_score(predicted: str, gold: str) -> float:
    """Token-level F1 (Squad-style). Empty gold = 0; empty pred = 0."""
    p_tokens = _tokenize(predicted)
    g_tokens = _tokenize(gold)
    if not g_tokens or not p_tokens:
        return 0.0
    common = Counter(p_tokens) & Counter(g_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(p_tokens)
    recall = num_same / len(g_tokens)
    return 2 * precision * recall / (precision + recall)


def _ngram(tokens: Sequence[str], n: int) -> List[tuple]:
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def bleu_score(
    predicted: str,
    gold: str,
    *,
    max_n: int = 4,
    brevity_penalty_floor: float = 0.1,
) -> float:
    """Sentence-BLEU with brevity penalty (no smoothing).

    Returns a value in [brevity_penalty_floor, 1.0] for non-empty inputs;
    0.0 for fully empty cases.
    """
    p_tokens = _tokenize(predicted)
    g_tokens = _tokenize(gold)
    if not g_tokens or not p_tokens:
        return 0.0
    log_precisions = []
    for n in range(1, max_n + 1):
        p_ngrams = Counter(_ngram(p_tokens, n))
        g_ngrams = Counter(_ngram(g_tokens, n))
        if not g_ngrams:
            break
        clipped = p_ngrams & g_ngrams
        num_same = sum(clipped.values())
        denom = max(1, sum(p_ngrams.values()))
        if num_same == 0:
            return 0.0
        import math

        log_precisions.append(math.log(num_same / denom))
    if not log_precisions:
        return 0.0
    import math

    bleu = math.exp(sum(log_precisions) / len(log_precisions))
    if len(p_tokens) < len(g_tokens):
        bp = math.exp(1.0 - len(g_tokens) / max(1, len(p_tokens)))
        bleu = bleu * max(brevity_penalty_floor, bp)
    return float(bleu)


# ---------------------------------------------------------------- aggregation


@dataclass
class CaseScore:
    """Per-QA scoring summary, used by the runner."""
    qa_id: str
    conv_id: str
    em: float
    f1: float
    bleu: float
    judge_score: Optional[float] = None
    judge_rationale: str = ""
    retrieved_text: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None


def _concat_top_k(retrieved: Sequence[RetrievedChunk], k: int = 3) -> str:
    parts = [c.text for c in list(retrieved)[:k] if c.text]
    return " \n".join(parts).strip()


def score_case(
    result: QAResult,
    *,
    k_for_judge: int = 3,
    judge_fn: Optional[Any] = None,
) -> CaseScore:
    """Score a single QAResult against its gold answer.

    The offline metrics (EM / F1 / BLEU) are computed against the
    *concatenated top-K retrieved text* — that mirrors how the judge
    LLM would see the context. ``judge_fn(qa, context)`` is optional;
    if absent or returning ``None`` the rubric score is left as ``None``.
    """
    gold = result.qa.answer
    context = _concat_top_k(result.retrieved, k=k_for_judge)
    em = exact_match(context, gold)
    f1 = f1_score(context, gold)
    bleu = bleu_score(context, gold)
    judge_score: Optional[float] = None
    rationale = ""
    if judge_fn is not None:
        try:
            payload = judge_fn(result.qa, context)
            if isinstance(payload, dict):
                judge_score = _coerce_score(payload.get("score"))
                rationale = str(payload.get("rationale") or "")
            elif isinstance(payload, (int, float)):
                judge_score = _coerce_score(payload)
        except Exception as exc:  # noqa: BLE001
            rationale = f"judge_fn error: {exc!r}"
    return CaseScore(
        qa_id=result.qa.qa_id,
        conv_id=result.qa.conv_id,
        em=em,
        f1=f1,
        bleu=bleu,
        judge_score=judge_score,
        judge_rationale=rationale,
        retrieved_text=context[:200],
        latency_ms=result.latency_ms,
        error=result.error,
    )


def _coerce_score(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def aggregate_metrics(scores: Iterable[CaseScore]) -> Dict[str, float]:
    """Mean + count + stddev over an iterable of CaseScore."""
    scores = list(scores)
    n = len(scores)
    if n == 0:
        return {"n": 0, "em": 0.0, "f1": 0.0, "bleu": 0.0, "judge": 0.0}
    em = sum(s.em for s in scores) / n
    f1 = sum(s.f1 for s in scores) / n
    bleu = sum(s.bleu for s in scores) / n
    judged = [s.judge_score for s in scores if s.judge_score is not None]
    judge = (sum(judged) / len(judged)) if judged else 0.0
    return {
        "n": float(n),
        "em": em,
        "f1": f1,
        "bleu": bleu,
        "judge": judge,
        "judged_count": float(len(judged)),
        "errors": float(sum(1 for s in scores if s.error)),
    }


# ---------------------------------------------------------------- LLM judge


DEFAULT_JUDGE_URL = os.getenv("JUDGE_LLM_URL", "http://localhost:8080/v1/chat/completions")
DEFAULT_JUDGE_MODEL = os.getenv("JUDGE_LLM_MODEL", "MiniMax-M3")
JUDGE_TIMEOUT = float(os.getenv("JUDGE_LLM_TIMEOUT", "30"))


JUDGE_PROMPT_TEMPLATE = (
    "You are a strict QA grader. Score the candidate answer against the gold answer.\n"
    "Score 0.0 if the candidate is wrong, 1.0 if it matches the gold, 0.5 if partially correct.\n"
    "Return JSON only: {{\"score\": <float>, \"rationale\": \"<short reason>\"}}.\n\n"
    "Question: {question}\n"
    "Gold answer: {gold}\n"
    "Candidate context (top-K retrieved chunks, may be noisy):\n{context}\n"
)


def judge_with_llm(
    qa: LocomoQAPair,
    context: str,
    *,
    base_url: str = DEFAULT_JUDGE_URL,
    model: str = DEFAULT_JUDGE_MODEL,
    timeout: float = JUDGE_TIMEOUT,
    client: Optional[httpx.Client] = None,
) -> Optional[Dict[str, Any]]:
    """Call the LLM-as-Judge endpoint. Returns ``None`` on any failure."""
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=qa.question,
        gold=qa.answer,
        context=context[:2000],
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict QA grader. Always return JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 200,
    }
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout)
    try:
        resp = client.post(base_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        import json as _json

        parsed = _json.loads(content)
        score = _coerce_score(parsed.get("score"))
        return {
            "score": score,
            "rationale": str(parsed.get("rationale") or ""),
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("judge_with_llm failed: %s", exc)
        return None
    finally:
        if own_client and client is not None:
            client.close()


__all__ = [
    "CaseScore",
    "JUDGE_PROMPT_TEMPLATE",
    "JUDGE_TIMEOUT",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_JUDGE_URL",
    "aggregate_metrics",
    "bleu_score",
    "exact_match",
    "f1_score",
    "judge_with_llm",
    "score_case",
]