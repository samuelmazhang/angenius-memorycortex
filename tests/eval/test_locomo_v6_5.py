"""v6.5.0 Phase 1 direction #4 — LOCOMO 1986 harness unit tests.

Four dispatch-required tests (mapping to spec §7.1 steps 1-4):

1. ``test_loader_parses_locomo_format`` — T-4.1 loader parses 10 convs
   and 1986 QA pairs (or surfaces a count drift warning).
2. ``test_adapter_feeds_cortex`` — T-4.2 adapter converts LOCOMO turns
   to Cortex ``/chunks/batch`` payloads, then runs QA through search_fn.
3. ``test_judge_returns_metrics`` — T-4.3 LLM-as-Judge returns EM / F1 /
   BLEU (offline path; the LLM call is mocked).
4. ``test_runner_reports_baseline`` — T-4.4 runner emits JSON + Markdown
   reports with baseline check.

Tests do not require a live Cortex server, Postgres, or MiniMax-M3 endpoint
— every external dep is monkeypatched via ``unittest.mock``.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.eval.cortex_adapter import (  # noqa: E402
    QAResult,
    RetrievedChunk,
    feed_conversation,
    run_qa,
    run_qa_batch,
)
from tests.eval.llm_judge import (  # noqa: E402
    aggregate_metrics,
    bleu_score,
    exact_match,
    f1_score,
    score_case,
)
from tests.eval.locomo_loader import (  # noqa: E402
    EXPECTED_TOTAL,
    LocomoDataset,
    LocomoQAPair,
    LocomoTurn,
    load_locomo,
)
from tests.eval.locomo_runner import (  # noqa: E402
    BASELINE_BLEU,
    BASELINE_EM,
    BASELINE_F1,
    run,
)


# ---------------------------------------------------------------- fixture


def _build_synthetic_locomo(per_conv: List[int], out_path: Path) -> Path:
    """Write a small synthetic LOCOMO-shaped JSON file (used by tests).

    snap-research's full 1986-case file is too large for CI; we generate
    a tiny version with the same shape (``{"conv-N": {"turns": [...], "qa": [...]}}``)
    so the loader / adapter / runner paths all exercise real code.
    """
    out = {}
    for idx, n_qa in enumerate(per_conv, start=1):
        conv_id = f"conv-{idx}"
        turns = [
            {"speaker": "Alice", "text": f"hello from Alice turn {i}", "timestamp": f"2026-01-0{idx}T00:0{i}:00Z"}
            for i in range(3)
        ] + [
            {"speaker": "Bob", "text": f"hi Alice, Bob here turn {i}", "timestamp": f"2026-01-0{idx}T00:0{i+1}:00Z"}
            for i in range(3)
        ]
        qa = []
        for i in range(n_qa):
            qa.append({
                "id": f"{conv_id}-q{i}",
                "question": f"Q{i} for {conv_id}: what did Alice say?",
                "answer": f"Alice said hello from Alice turn {i % 3}",
                "category": "single_hop" if i % 2 == 0 else "multi_hop",
            })
        out[conv_id] = {
            "conv_id": conv_id,
            "sample_observation": {"speaker_a": "Alice", "speaker_b": "Bob"},
            "turns": turns,
            "qa": qa,
        }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _stub_search_factory(pool: List[Dict[str, Any]]):
    """Return a search_fn that pops a chunk from a pool for every query."""
    cursor = {"i": 0}

    def _fn(query: str, top_k: int) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for k in range(top_k):
            idx = cursor["i"] % len(pool)
            base = dict(pool[idx])
            base = dict(base)
            base["memory_id"] = f"{base.get('memory_id', 'm')}-{cursor['i']}-{k}"
            base["score"] = round(1.0 - (k * 0.05), 4)
            hits.append(base)
            cursor["i"] += 1
        return hits

    return _fn


# ---------------------------------------------------------------- T-4.1


class TestLoaderParsesLocomoFormat(unittest.TestCase):
    """Spec step 1: loader parses 10 convs, 1986 QA pairs (or warns on drift)."""

    def setUp(self):
        self.tmp = Path(self._tempdir()) / "locomo_small.json"
        # Small synthetic dataset: 3 convs × 4 QA each = 12 cases. Counts are
        # intentionally below 1986 so we exercise the drift-warning branch.
        _build_synthetic_locomo([4, 4, 4], self.tmp)
        self.dataset = load_locomo(self.tmp)

    @staticmethod
    def _tempdir():
        import tempfile

        return tempfile.mkdtemp(prefix="locomo-test-")

    def test_loader_indexes_by_conv_id(self):
        """Loader 必须按 conv_id 索引对话, 且 qa 索引可按 qa_id 查."""
        ids = self.dataset.all_conversation_ids()
        self.assertEqual(sorted(ids), ["conv-1", "conv-2", "conv-3"])
        conv = self.dataset.get_conversation("conv-1")
        self.assertIsNotNone(conv)
        self.assertGreater(len(conv.turns), 0)
        self.assertGreater(len(conv.qa_pairs), 0)

    def test_loader_counts_qa_pairs(self):
        """Loader 必须把所有 conv 的 QA pair 汇总, 总数 = sum(per_conv)."""
        total = self.dataset.total_qa()
        self.assertEqual(total, sum(4 for _ in range(3)))
        # Per-conv counts match input.
        per_conv_actual = [len(self.dataset.get_conversation(c).qa_pairs) for c in ["conv-1", "conv-2", "conv-3"]]
        self.assertEqual(per_conv_actual, [4, 4, 4])

    def test_loader_surfaces_1986_baseline(self):
        """EXPECTED_TOTAL = 1986 (PRD §7 关键修正) 且 module 暴露该常量."""
        self.assertEqual(EXPECTED_TOTAL, 1986)
        # Loader must emit a warning on count drift (12 < 1986).
        with self.assertLogs("tests.eval.locomo_loader", level="WARNING") as cm:
            load_locomo(self.tmp)
        joined = "\n".join(cm.output)
        self.assertIn("drift", joined.lower())

    def test_loader_turns_have_speaker(self):
        """每个 turn 必须有 speaker 字段, 文本可读."""
        conv = self.dataset.get_conversation("conv-1")
        for t in conv.turns:
            self.assertIn(t.speaker, ("Alice", "Bob"))
            self.assertTrue(t.text)


# ---------------------------------------------------------------- T-4.2


class TestAdapterFeedsCortex(unittest.TestCase):
    """Spec step 2: adapter 把 LOCOMO turn 喂入 Cortex + 跑 QA."""

    def setUp(self):
        self.tmp = Path(self._tempdir()) / "locomo_small.json"
        _build_synthetic_locomo([3], self.tmp)
        self.dataset = load_locomo(self.tmp)
        self.conv = self.dataset.get_conversation("conv-1")
        self.qa = self.dataset.qa_index["conv-1-q0"]
        self.chunk_pool = [
            {
                "memory_id": "m-1",
                "content": "Alice said hello from Alice turn 0",
                "importance": 6,
                "speaker_name": "Alice",
            },
            {
                "memory_id": "m-2",
                "content": "Alice's favorite color is blue",
                "importance": 5,
                "speaker_name": "Alice",
            },
        ]

    @staticmethod
    def _tempdir():
        import tempfile

        return tempfile.mkdtemp(prefix="locomo-test-")

    def test_adapter_post_chunks_batch(self):
        """feed_conversation() 必须 POST 到 /chunks/batch, chunks 数组对齐 turns."""
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"written": len(self.conv.turns)}
        resp.content = True
        client.post.return_value = resp

        report = feed_conversation(self.conv, client=client, importance=7)
        client.post.assert_called_once()
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], "/chunks/batch")
        self.assertEqual(kwargs["json"]["chunks"][0]["importance"], 7)
        self.assertEqual(kwargs["json"]["chunks"][0]["speaker_name"], "Alice")
        self.assertEqual(report.chunks_written, len(self.conv.turns))
        self.assertIn("Alice", report.by_speaker)

    def test_adapter_run_qa_via_search_fn(self):
        """run_qa() 必须用 search_fn 拿 top-K, 包装成 QAResult."""
        search_fn = _stub_search_factory(self.chunk_pool)
        result = run_qa(self.qa, top_k=2, search_fn=search_fn)
        self.assertIsInstance(result, QAResult)
        self.assertEqual(result.qa.qa_id, self.qa.qa_id)
        self.assertEqual(len(result.retrieved), 2)
        self.assertIsInstance(result.retrieved[0], RetrievedChunk)
        self.assertGreater(result.latency_ms, 0)
        self.assertIsNone(result.error)

    def test_adapter_run_qa_batch_returns_list(self):
        """run_qa_batch() 必须一次跑完所有 qa pair, 每个有 QAResult."""
        qas = list(self.dataset.qa_index.values())
        results = run_qa_batch(qas, top_k=2, search_fn=_stub_search_factory(self.chunk_pool))
        self.assertEqual(len(results), len(qas))
        for r in results:
            self.assertIsInstance(r, QAResult)


# ---------------------------------------------------------------- T-4.3


class TestJudgeReturnsMetrics(unittest.TestCase):
    """Spec step 3: LLM-as-Judge 返回 EM / F1 / BLEU (LLM call mocked)."""

    def setUp(self):
        self.qa = LocomoQAPair(
            conv_id="conv-1",
            qa_id="conv-1-q0",
            question="What did Alice say?",
            answer="Alice said hello from Alice turn 0",
            category="single_hop",
        )
        retrieved = [
            RetrievedChunk(
                memory_id="m-1",
                text="Alice said hello from Alice turn 0",
                score=0.95,
            ),
            RetrievedChunk(
                memory_id="m-2",
                text="Bob responded and they talked about weather.",
                score=0.40,
            ),
        ]
        self.result = QAResult(qa=self.qa, retrieved=retrieved, latency_ms=12.0)

    def test_metrics_helper_correctness(self):
        """f1 / bleu / em 在干净输入上必须 >= 0.5."""
        self.assertGreaterEqual(exact_match("hello world", "hello world"), 1.0)
        self.assertEqual(exact_match("hello world", "goodbye"), 0.0)
        self.assertGreater(f1_score("the quick brown fox", "the quick fox"), 0.5)
        self.assertGreater(bleu_score("hello world", "hello world"), 0.9)

    def test_score_case_offline(self):
        """score_case() 走 offline 路径, 不打 LLM, 返回 F1/EM/BLEU 数值."""
        # Use a single-chunk result so EM=1.0 (gold == top-1).
        single = QAResult(
            qa=self.qa,
            retrieved=[RetrievedChunk("m-1", "Alice said hello from Alice turn 0", 0.95)],
            latency_ms=12.0,
        )
        score = score_case(single)
        self.assertGreater(score.f1, 0.5)
        self.assertGreaterEqual(score.em, 1.0)  # gold text == top-1 chunk
        self.assertGreater(score.bleu, 0.5)
        self.assertIsNone(score.judge_score)  # no judge_fn

    def test_score_case_with_mocked_judge(self):
        """score_case() 接受 judge_fn, 把 LLM 返的 score/rationale 透传."""
        def judge_fn(qa, ctx):
            return {"score": 0.75, "rationale": "mostly correct"}

        score = score_case(self.result, judge_fn=judge_fn)
        self.assertEqual(score.judge_score, 0.75)
        self.assertEqual(score.judge_rationale, "mostly correct")

    def test_judge_with_llm_mocked_http(self):
        """judge_with_llm() 打 httpx, 解析 score, 不抛."""
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"score": 0.6, "rationale": "ok"})}}]
        }
        client = MagicMock()
        client.post.return_value = fake_resp
        out = __import__("tests.eval.llm_judge", fromlist=["judge_with_llm"]).judge_with_llm(
            self.qa, "ctx", client=client
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["score"], 0.6)

    def test_aggregate_metrics_keys(self):
        """aggregate_metrics() 必须返 n / em / f1 / bleu / judge / errors / judged_count."""
        scores = [
            score_case(self.result),
            score_case(
                QAResult(
                    qa=self.qa,
                    retrieved=[RetrievedChunk("m-3", "completely unrelated", 0.1)],
                    latency_ms=5.0,
                )
            ),
        ]
        agg = aggregate_metrics(scores)
        for key in ("n", "em", "f1", "bleu", "judge", "judged_count", "errors"):
            self.assertIn(key, agg)
        self.assertEqual(agg["n"], 2.0)


# ---------------------------------------------------------------- T-4.4


class TestRunnerReportsBaseline(unittest.TestCase):
    """Spec step 4: runner 输出 JSON + Markdown 报告 + baseline 校验."""

    def setUp(self):
        self.tmp_data = Path(self._tempdir()) / "locomo_small.json"
        _build_synthetic_locomo([4, 4], self.tmp_data)
        self.tmp_report = Path(self._tempdir()) / "report.json"
        self.tmp_md = Path(self._tempdir()) / "report.md"
        self.chunk_pool = [
            {"memory_id": "m-1", "content": "Alice said hello from Alice turn 0"},
            {"memory_id": "m-2", "content": "Alice said hello from Alice turn 1"},
        ]

    @staticmethod
    def _tempdir():
        import tempfile

        return tempfile.mkdtemp(prefix="locomo-test-")

    def test_runner_emits_json_and_md(self):
        """run() 必须写 .json + .md 报告, 含 metrics / baseline / samples."""
        report = run(
            data_path=str(self.tmp_data),
            max_cases=8,
            top_k=2,
            report_path=str(self.tmp_report),
            md_path=str(self.tmp_md),
            search_fn=_stub_search_factory(self.chunk_pool),
        )
        self.assertTrue(self.tmp_report.exists())
        self.assertTrue(self.tmp_md.exists())
        # 关键字段都在
        for key in ("metrics", "passes_baseline", "baseline", "profile", "samples", "config"):
            self.assertIn(key, report)
        self.assertIn("f1", report["metrics"])
        # 8 case (4+4) 全跑过
        self.assertEqual(int(report["metrics"]["n"]), 8)
        # baseline 字段是 v6.5.0 设定的 floor
        self.assertEqual(report["baseline"]["f1"], BASELINE_F1)
        self.assertEqual(report["baseline"]["em"], BASELINE_EM)
        self.assertEqual(report["baseline"]["bleu"], BASELINE_BLEU)

    def test_runner_md_has_metrics_table(self):
        """Markdown 报告必须含 F1 / EM / BLEU / baseline 表 + PASS/FAIL 状态行."""
        run(
            data_path=str(self.tmp_data),
            max_cases=4,
            top_k=1,
            report_path=str(self.tmp_report),
            md_path=str(self.tmp_md),
            search_fn=_stub_search_factory(self.chunk_pool),
        )
        md = self.tmp_md.read_text(encoding="utf-8")
        self.assertIn("## Metrics", md)
        self.assertIn("F1", md)
        self.assertIn("EM", md)
        self.assertIn("BLEU", md)
        self.assertIn("baseline", md.lower())
        self.assertIn("Status:", md)


if __name__ == "__main__":
    unittest.main()