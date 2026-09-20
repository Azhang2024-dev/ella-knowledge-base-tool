# -*- coding: utf-8 -*-
"""Unit tests for _parse_relevance_scores fail-closed score handling."""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from core.doc_processor import _parse_relevance_scores  # noqa: E402


class TestParseRelevanceScores(unittest.TestCase):
    def test_explicit_scores_kept(self):
        raw = '[{"id": 0, "score": 4}, {"id": 2, "score": 3}, {"id": 5, "score": 2}]'
        items = _parse_relevance_scores(raw)
        self.assertEqual(items, [
            {"id": 0, "score": 4},
            {"id": 2, "score": 3},
            {"id": 5, "score": 2},
        ])

    def test_missing_score_skipped(self):
        raw = '[{"id": 0, "score": 4}, {"id": 1}, {"id": 2, "score": 2}]'
        items = _parse_relevance_scores(raw)
        self.assertEqual(items, [
            {"id": 0, "score": 4},
            {"id": 2, "score": 2},
        ])

    def test_bare_int_list_yields_empty(self):
        raw = "[0, 2, 5]"
        self.assertEqual(_parse_relevance_scores(raw), [])

    def test_mixed_bare_and_dict(self):
        raw = '[0, {"id": 1, "score": 3}, 2]'
        self.assertEqual(_parse_relevance_scores(raw), [{"id": 1, "score": 3}])


if __name__ == "__main__":
    unittest.main()
