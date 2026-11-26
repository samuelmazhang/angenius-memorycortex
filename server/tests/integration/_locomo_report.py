"""Render LOCOMO recall results as a human-readable report.

Reads the JSON output of ``_locomo_recall.py`` and prints:
  * Overall A vs C comparison (recall + precision)
  * Per-category breakdown (LOCOMO categories 1-5)
  * Lift summary (v6.5.0 advantage over FTS5-only)

The report frame is "memory engine quality", not "answer
quality". Recall answers "did we find it?" and precision answers
"did we mix in noise?". Together they map to the cortex
design goal: precise retrieval from large memory with no
relevant chunk missed AND no irrelevant chunk returned.

The output is plain text + a markdown block. Pass --md-out to
also write a standalone markdown report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def render(data: dict) -> str:
    n = data["n_queries"]
    a = data["A"]
    c = data["C"]
    per_cat = data["per_category"]

    lines = []
    lines.append(f"# Cortex v6.5.0 LOCOMO-1986 Memory Quality Report (n={n} queries)\n")
    lines.append("Two numbers answer the two halves of the design goal:\n")
    lines.append("  * **recall@K** = did we find the relevant chunk?\n"
                 "    (i.e. 'do not miss necessary memory')\n")
    lines.append("  * **precision@K** = is the top-K clean of noise?\n"
                 "    (i.e. 'do not return irrelevant content')\n")
    lines.append("")

    lines.append("## Strategy summary\n")
    lines.append("| Strategy | recall@1 | recall@3 | recall@5 | recall@10 | precision@1 | precision@3 | precision@5 | precision@10 | MRR | p50 ms")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    lines.append(
        "| **A. v6.5.0 (Nomic + FTS5 hybrid)** | "
        + f"{fmt_pct(a['recall_at_1'])} | "
        + f"{fmt_pct(a['recall_at_3'])} | "
        + f"{fmt_pct(a['recall_at_5'])} | "
        + f"{fmt_pct(a['recall_at_10'])} | "
        + f"{fmt_pct(a['precision_at_1'])} | "
        + f"{fmt_pct(a['precision_at_3'])} | "
        + f"{fmt_pct(a['precision_at_5'])} | "
        + f"{fmt_pct(a['precision_at_10'])} | "
        + f"{a['mrr']:.3f} | "
        + f"{a['latency_p50_ms']:.0f} |"
    )
    lines.append(
        "| C. FTS5 only (PG direct)         | "
        + f"{fmt_pct(c['recall_at_1'])} | "
        + f"{fmt_pct(c['recall_at_3'])} | "
        + f"{fmt_pct(c['recall_at_5'])} | "
        + f"{fmt_pct(c['recall_at_10'])} | "
        + f"{fmt_pct(c['precision_at_1'])} | "
        + f"{fmt_pct(c['precision_at_3'])} | "
        + f"{fmt_pct(c['precision_at_5'])} | "
        + f"{fmt_pct(c['precision_at_10'])} | "
        + f"{c['mrr']:.3f} | "
        + f"{c['latency_p50_ms']:.0f} |"
    )
    lines.append("")

    lines.append("## Lift (v6.5.0 vector advantage over FTS5-only)\n")
    lines.append(f"- **recall@5 lift**: {fmt_pct(a['recall_at_5'] - c['recall_at_5'])} "
                 f"({fmt_pct(c['recall_at_5'])} -> {fmt_pct(a['recall_at_5'])})")
    lines.append(f"- **recall@10 lift**: {fmt_pct(a['recall_at_10'] - c['recall_at_10'])}")
    lines.append(f"- **precision@5 lift**: {fmt_pct(a['precision_at_5'] - c['precision_at_5'])} "
                 f"({fmt_pct(c['precision_at_5'])} -> {fmt_pct(a['precision_at_5'])})")
    lines.append(f"- **precision@10 lift**: {fmt_pct(a['precision_at_10'] - c['precision_at_10'])}")
    lines.append(f"- **MRR lift**: {a['mrr']-c['mrr']:+.3f}")
    lines.append("")

    lines.append("## Per-category breakdown\n")
    lines.append("LOCOMO categories: 1-hop, 2-hop, temporal, open-domain, adversarial.")
    lines.append("")
    lines.append("| Cat | n | A R@5 | A P@5 | C R@5 | C P@5 | A MRR | C MRR")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cat, m in sorted(per_cat.items(), key=lambda kv: kv[0]):
        if m["n"] == 0:
            continue
        a_r5 = m["A_recall_at_5"]
        c_r5 = m["C_recall_at_5"]
        a_p5 = m["A_precision_at_5"]
        c_p5 = m["C_precision_at_5"]
        a_mrr = m["A_mrr"]
        c_mrr = m["C_mrr"]
        lines.append(
            f"| {cat} | {m['n']} | "
            f"{fmt_pct(a_r5)} | {fmt_pct(a_p5)} | "
            f"{fmt_pct(c_r5)} | {fmt_pct(c_p5)} | "
            f"{a_mrr:.3f} | {c_mrr:.3f}"
        )
    lines.append("")

    lines.append("## Reading the table\n")
    lines.append("- **recall@K**: fraction of queries where ANY of the top-K "
                 "hits' `metadata.dia_id` is in the QA's evidence list.\n"
                 "  Design-goal mapping: 'do not miss necessary memory'.\n")
    lines.append("- **precision@K**: fraction of top-K hits whose "
                 "`metadata.dia_id` is in the evidence list.\n"
                 "  Design-goal mapping: 'do not return irrelevant content'.\n")
    lines.append("- **MRR**: mean reciprocal rank of the first correct hit.\n")
    lines.append("- **Latency**: client-measured round trip (A: HTTP, C: in-process psycopg2).")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("results", type=str)
    p.add_argument("--md-out", type=str, default=None)
    args = p.parse_args()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    md = render(data["summary"])
    print(md)
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
        print(f"\nWrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())