"""B-layer integration test: scenario 4.

Scenario 4 -- RAPTOR + consolidate.summarize_chunks: unified summarize_fn
contract after ADR-XXX.

Cross-module:
  * raptor.build_tree (Phase 2 #9) -- summarize_fn(texts: list[str]) -> str
  * consolidate.summarize_chunks (Phase 1 #C) -- same contract (v6.5.1+)

In v6.5.0 the two production modules exposed hooks with DIFFERENT type
signatures:

  * consolidate.summary.summarize_chunks -> passed list[Dict]
    ({"id", "content", "importance"})
  * raptor.build_tree                  -> passed list[str]

This forced every operator who wired one function for both call sites
to write a thin shim. ADR-XXX unified both contracts on ``list[str]``.

What we verify (after the unification):
  1. RAPTOR's summarize_fn hook receives ``list[str]``.
  2. consolidate.summarize_chunks's summarize_fn hook also receives
     ``list[str]`` -- the v6.5.1 unified contract.
  3. ``adapt_dict_summarize_fn`` lets a v6.5.0-style dict function
     still work (backward-compat adapter).
  4. The same ``list[str]``-typed function works against both call
     sites without any shim -- the main win of the unification.
"""

from __future__ import annotations

import unittest

from _shared import (  # type: ignore[import-not-found]
    _chunk,
    adapt_dict_summarize_fn,
    build_tree,
    summarize_chunks,
)


class _TextListSummarizeFn:
    """Unified contract (v6.5.1+): ``summarize_fn(texts: list[str]) -> str``."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, texts):
        texts_list = list(texts) if texts is not None else []
        self.calls.append(texts_list)
        return f"<sum:{len(texts_list)}>"


class TestSummaryRaptorContract(unittest.TestCase):
    """Pins the unified summarize_fn contract (ADR-XXX)."""

    def test_raptor_summarize_fn_contract_is_list_of_strings(self):
        chunks = [
            _chunk("hot0", "hot content 0"),
            _chunk("hot1", "hot content 1"),
            _chunk("cold0", "cold content 0"),
            _chunk("cold1", "cold content 1"),
        ]
        fn = _TextListSummarizeFn()
        tree = build_tree(chunks, summarize_fn=fn, cluster_threshold=2)
        self.assertGreater(len(tree.leaf_ids), 0)
        self.assertGreaterEqual(len(fn.calls), 1,
            "RAPTOR.build_tree never invoked the injected summarize_fn.")
        for payload in fn.calls:
            self.assertIsInstance(payload, list)
            for item in payload:
                self.assertIsInstance(item, str,
                    f"RAPTOR handed a non-str to summarize_fn: {type(item).__name__}. "
                    "The unified contract is list[str] -- if you change it, "
                    "update this test AND the adapter.")

    def test_consolidate_summarize_fn_contract_is_list_of_strings(self):
        """v6.5.1+: consolidate.summarize_chunks also passes list[str]."""
        chunks = [
            {"id": "a", "content": "alpha", "importance": 7.0},
            {"id": "b", "content": "beta", "importance": 5.0},
            {"id": "c", "content": "gamma", "importance": 9.0},
        ]
        fn = _TextListSummarizeFn()
        out = summarize_chunks(chunks, summarize_fn=fn)
        self.assertIsInstance(out, str)
        self.assertEqual(len(fn.calls), 1)
        payload = fn.calls[0]
        # Unified contract: list[str].
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 3)
        for item in payload:
            self.assertIsInstance(item, str,
                "summarize_chunks should pass list[str] per ADR-XXX. "
                "If you intentionally regressed to list[Dict], use "
                "adapt_dict_summarize_fn() at the call site instead.")
        # Texts come from .content fields, in importance-desc / id-asc order:
        # gamma (9.0) > alpha (7.0) > beta (5.0)
        self.assertEqual(payload, ["gamma", "alpha", "beta"])

    def test_one_function_works_for_both_call_sites(self):
        """The main win of the unification: write ONE summarize_fn,
        use it for both RAPTOR + summarize_chunks without a shim.
        """
        chunks = [
            {"id": "a", "content": "alpha", "importance": 7.0},
            {"id": "b", "content": "beta", "importance": 5.0},
        ]
        fn = _TextListSummarizeFn()
        # Call site 1: RAPTOR (chunk dicts -> texts)
        build_tree(
            [_chunk("a", "alpha"), _chunk("b", "beta")],
            summarize_fn=fn, cluster_threshold=2,
        )
        raptor_calls = list(fn.calls)
        # Call site 2: summarize_chunks (chunk dicts -> texts)
        out = summarize_chunks(chunks, summarize_fn=fn)
        # Both call sites invoked fn at least once.
        self.assertGreaterEqual(len(raptor_calls), 1)
        self.assertEqual(len(fn.calls) - len(raptor_calls), 1)
        last_payload = fn.calls[-1]
        self.assertEqual(last_payload, ["alpha", "beta"])
        self.assertTrue(out.startswith("<sum:"))

    def test_adapt_dict_summarize_fn_backward_compat(self):
        """v6.5.0-style dict-based summarize_fn still works via
        adapt_dict_summarize_fn.
        """
        received_dicts: list = []

        def legacy_dict_fn(dicts):
            received_dicts.append(list(dicts))
            return f"<legacy:{len(dicts)}>"

        adapted = adapt_dict_summarize_fn(legacy_dict_fn)
        # Adapted function is callable with list[str]:
        out = adapted(["alpha", "beta", "gamma"])
        # The legacy function received 3 dicts with .content populated.
        self.assertEqual(len(received_dicts), 1)
        self.assertEqual(len(received_dicts[0]), 3)
        for d in received_dicts[0]:
            self.assertIn("content", d)
            self.assertIn("importance", d)
        self.assertEqual(out, "<legacy:3>")
        # Now wire the adapted fn into summarize_chunks (the v6.5.1
        # caller passes list[str], the adapter rebuilds dicts for
        # legacy_dict_fn).
        out2 = summarize_chunks(
            [
                {"id": "a", "content": "alpha", "importance": 7.0},
                {"id": "b", "content": "beta", "importance": 5.0},
            ],
            summarize_fn=adapted,
        )
        self.assertEqual(out2, "<legacy:2>")
        self.assertEqual(len(received_dicts), 2)
        self.assertEqual([d["content"] for d in received_dicts[1]], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()