"""P2.8: query-gen baseline temperature is 0.0; creative opt-in keeps 0.7."""
from __future__ import annotations

import unittest

from core.doc_processor import _query_gen_seed, _query_gen_temperature


class TestQueryGenTemperature(unittest.TestCase):
    def test_baseline_is_zero(self):
        self.assertEqual(_query_gen_temperature({}), 0.0)
        self.assertEqual(_query_gen_temperature({"creative": False}), 0.0)

    def test_creative_flag_uses_0_7(self):
        self.assertEqual(_query_gen_temperature({"creative": True}), 0.7)
        self.assertEqual(_query_gen_temperature({"query_gen_creative": True}), 0.7)

    def test_explicit_override(self):
        self.assertEqual(_query_gen_temperature({"query_gen_temperature": 0.2}), 0.2)
        self.assertEqual(
            _query_gen_temperature({"creative": True, "query_gen_temperature_creative": 0.9}),
            0.9,
        )

    def test_seed_defaults_to_zero(self):
        self.assertEqual(_query_gen_seed({}), 0)
        self.assertEqual(_query_gen_seed({"seed": 42}), 42)
        self.assertEqual(_query_gen_seed({"query_gen_seed": 7}), 7)


if __name__ == "__main__":
    unittest.main()
