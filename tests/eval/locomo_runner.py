"""v6.5.0 Phase 1 direction #4 — LOCOMO runner.

Orchestrates the full pipeline:

1. Load LOCOMO via :mod:`tests.eval.locomo_loader`
2. (optionally) feed conversations to Cortex via :mod:`tests.eval.cortex_adapter`
3. Run the sampled QA pairs through Cortex search
4. Score via :mod:`tests.eval.llm_judge` (offline metrics + optional LLM)
5. Emit a JSON report + a Markdown summary

Two run profiles:

* **CI (default)** — 200-case sample, ~5 minutes, no LLM-as-Judge.
* **Nightly** — full 1986-case set, optional LLM judge, ~30 minutes.

Spec mapping (PRD §7, direction #4):
- Step 4 (CI runner + workflow): tests/eval/locomo_runner.py + .github/workflows/locomo-ci.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# Allow `python tests/eval/locomo_runner.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.eval.cortex_adapter import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_TOP_K,
    QAResult,
    feed_conversation,
    run_qa,
    run_qa_batch,
)
from tests.eval.llm_judge import (  # noqa: E402
    CaseScore,
    aggregate_metrics,
    judge_with_llm,
    score_case,
)
from tests.eval.locomo_loader import (  # noqa: E402
    EXPECTED_TOTAL,
    LocomoDataset,
    LocomoQAPair,
    load_locomo,
)

logger = logging.getLogger(__name__)

DEFAULT_DATA_PATH = "data/locomo10.json"
DEFAULT_REPORT_PATH = ".sprint/locomo-report.json"
DEFAULT_MD_PATH = ".sprint/locomo-report.md"

# v6.5.0 baseline (PRD §7.3, §13.2) — Mem0 / A-MEM LOCOMO F1 ~0.65-0.72
# on the same dataset. We anchor against 0.50 floor for first release.
BASELINE_F1 = 0.50
BASELINE_EM = 0.30
BASELINE_BLEU = 0.30


def make_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LOCOMO 1986 case runner")
    p.add_argument("--data", default=DEFAULT_DATA_PATH, help="LOCOMO json file")
    p.add_argument("--max-cases", type=int, default=200, help="Cap (CI=200, nightly=0=all)")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--report", default=DEFAULT_REPORT_PATH)
    p.add_argument("--md", default=DEFAULT_MD_PATH)
    p.add_argument("--with-judge", action="store_true", help="Enable LLM-as-Judge")
    p.add_argument("--judge-url", default=os.getenv("JUDGE_LLM_URL", "http://localhost:8080/v1/chat/completions"))
    p.add_argument("--judge-model", default=os.getenv("JUDGE_LLM_MODEL", "MiniMax-M3"))
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--conv-ids", nargs="*", default=None, help="Restrict to a subset of conv_ids")
    p.add_argument(
        "--search-fn",
        default=None,
        help="Optional import path 'pkg.module:function' to use as in-process search backend (skips HTTP).",
    )
    p.add_argument("--verbose", action="store_true")
    return p


def _resolve_search_fn(spec: Optional[str]) -> Optional[Callable[[str, int], List[Dict[str, Any]]]]:
    if not spec:
        return None
    mod_name, _, fn_name = spec.partition(":")
    if not mod_name or not fn_name:
        raise ValueError(f"--search-fn expects 'module:function', got {spec!r}")
    import importlib

    mod = importlib.import_module(mod_name)
    fn = getattr(mod, fn_name)
    if not callable(fn):
        raise TypeError(f"{spec} resolved to non-callable {fn!r}")
    return fn


def _emit_markdown(report: Dict[str, Any], path: Path) -> None:
    metrics = report["metrics"]
    lines: List[str] = [
        f"# LOCOMO Eval Report ({report['profile']})",
        "",
        f"- Generated: {report['timestamp']}",
        f"- Profile: `{report['profile']}` (max_cases={report['config']['max_cases']})",
        f"- Top-K: {report['config']['top_k']}",
        f"- Data: `{report['config']['data']}`",
        "",
        "## Metrics",
        "",
        "| metric | value | baseline |",
        "|---|---|---|",
        f"| n        | {int(metrics['n'])} | — |",
        f"| F1       | {metrics['f1']:.4f} | {BASELINE_F1:.2f} |",
        f"| EM       | {metrics['em']:.4f} | {BASELINE_EM:.2f} |",
        f"| BLEU     | {metrics['bleu']:.4f} | {BASELINE_BLEU:.2f} |",
        f"| Judge    | {metrics['judge']:.4f} | — |",
        f"| Errors   | {int(metrics['errors'])} | — |",
        f"| Judged   | {int(metrics.get('judged_count', 0))} | — |",
        "",
        "## Per-case samples (first 5)",
        "",
        "| qa_id | EM | F1 | BLEU | judge | latency_ms |",
        "|---|---|---|---|---|---|",
    ]
    for s in report.get("samples", [])[:5]:
        j = "" if s.get("judge_score") is None else f"{s['judge_score']:.2f}"
        lines.append(
            f"| `{s['qa_id']}` | {s['em']:.2f} | {s['f1']:.2f} | {s['bleu']:.2f} | {j} | {s['latency_ms']:.0f} |"
        )
    lines.extend(
        [
            "",
            "## Baseline",
            "",
            f"- F1 floor: {BASELINE_F1}",
            f"- EM floor: {BASELINE_EM}",
            f"- BLEU floor: {BASELINE_BLEU}",
            "",
            f"Status: {'PASS' if _passes_baseline(metrics) else 'BELOW BASELINE'}",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _passes_baseline(metrics: Dict[str, float]) -> bool:
    return (
        metrics["f1"] >= BASELINE_F1
        and metrics["em"] >= BASELINE_EM
        and metrics["bleu"] >= BASELINE_BLEU
    )


def run(
    data_path: str = DEFAULT_DATA_PATH,
    *,
    max_cases: int = 200,
    top_k: int = DEFAULT_TOP_K,
    base_url: str = DEFAULT_BASE_URL,
    report_path: str = DEFAULT_REPORT_PATH,
    md_path: str = DEFAULT_MD_PATH,
    with_judge: bool = False,
    judge_url: str = "",
    judge_model: str = "MiniMax-M3",
    conv_ids: Optional[Sequence[str]] = None,
    search_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    feed: bool = False,
) -> Dict[str, Any]:
    """Run the LOCOMO harness end-to-end. Returns the JSON report dict."""
    started = time.perf_counter()
    dataset = load_locomo(data_path)
    qa_iter = dataset.iter_qa_subset(
        max_cases=max_cases if max_cases > 0 else None,
        conv_ids=conv_ids,
    )
    qa_pairs: List[LocomoQAPair] = list(qa_iter)
    logger.info("loaded %d QA pairs from %s", len(qa_pairs), data_path)

    if feed:
        for conv in dataset.conversations.values():
            feed_conversation(conv, base_url=base_url)
            logger.info("fed conv %s (%d turns)", conv.conv_id, len(conv.turns))

    results: List[QAResult] = run_qa_batch(
        qa_pairs, top_k=top_k, search_fn=search_fn, base_url=base_url
    )
    logger.info("retrieved top-%d for %d QA pairs", top_k, len(results))

    judge_fn: Optional[Callable[[LocomoQAPair, str], Any]] = None
    if with_judge:
        def _fn(qa: LocomoQAPair, ctx: str) -> Any:
            return judge_with_llm(qa, ctx, base_url=judge_url, model=judge_model)
        judge_fn = _fn

    scores: List[CaseScore] = [
        score_case(r, judge_fn=judge_fn) for r in results
    ]
    metrics = aggregate_metrics(scores)

    report: Dict[str, Any] = {
        "profile": "ci-200" if max_cases == 200 else ("nightly-1986" if max_cases in (0, 1986) else f"custom-{max_cases}"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "data": data_path,
            "max_cases": max_cases,
            "top_k": top_k,
            "with_judge": with_judge,
        },
        "metrics": metrics,
        "samples": [asdict(s) for s in scores[:5]],
        "passes_baseline": _passes_baseline(metrics),
        "baseline": {
            "f1": BASELINE_F1,
            "em": BASELINE_EM,
            "bleu": BASELINE_BLEU,
        },
        "elapsed_sec": time.perf_counter() - started,
    }

    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit_markdown(report, Path(md_path))
    logger.info(
        "report written: %s (F1=%.4f EM=%.4f BLEU=%.4f, %d cases, %.1fs)",
        report_path,
        metrics["f1"],
        metrics["em"],
        metrics["bleu"],
        int(metrics["n"]),
        report["elapsed_sec"],
    )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = make_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    search_fn = _resolve_search_fn(args.search_fn)
    run(
        data_path=args.data,
        max_cases=args.max_cases,
        top_k=args.top_k,
        base_url=args.base_url,
        report_path=args.report,
        md_path=args.md,
        with_judge=args.with_judge,
        judge_url=args.judge_url,
        judge_model=args.judge_model,
        conv_ids=args.conv_ids,
        search_fn=search_fn,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())