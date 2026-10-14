"""Offline guard tests; never contact a provider."""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from evaluate_blog_live import BudgetExhausted, CostGuard, peak_cny


class FakeResponses:
    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(status="completed", usage=self.usage)


class LiveBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "budget.json"

    async def test_peak_price_counts_reasoning_in_output_once(self):
        self.assertAlmostEqual(peak_cny(1_000_000, 1_000_000, 0), 12.0)
        self.assertAlmostEqual(peak_cny(1_000_000, 0, 1_000_000), 0.048)

    async def test_reserve_before_call_and_persist_for_restart(self):
        fake = FakeResponses(SimpleNamespace(input_tokens=100, output_tokens=50,
                                              input_tokens_details=SimpleNamespace(cached_tokens=20)))
        guard = CostGuard(self.path, 5.0)
        await guard.create(fake, instructions="test", input=[], tools=[], reasoning={"effort": "low"},
                           max_output_tokens=100)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(guard.ledger["requests"][0]["status"], "usage_observed")
        self.assertEqual(len(CostGuard(self.path, 5.0).ledger["requests"]), 1)
        with self.assertRaises(ValueError):
            CostGuard(self.path, 6.0)

    async def test_over_cap_rejects_before_network(self):
        fake = FakeResponses(None)
        guard = CostGuard(self.path, 5.0)
        with self.assertRaises(BudgetExhausted):
            await guard.create(fake, instructions="test", input=[], tools=[], reasoning={},
                               max_output_tokens=1_000_000)
        self.assertEqual(fake.calls, 0)
        self.assertFalse(self.path.exists())

    async def test_missing_usage_keeps_full_reservation(self):
        fake = FakeResponses(None)
        guard = CostGuard(self.path, 5.0)
        await guard.create(fake, instructions="test", input=[], tools=[], reasoning={},
                           max_output_tokens=100)
        self.assertEqual(guard.ledger["requests"][0]["status"], "usage_missing")
        self.assertEqual(guard.ledger["used_cny_upper"], guard.ledger["requests"][0]["charged_cny_upper"])

    async def test_half_yuan_margin_allows_remaining_work_without_crossing_cap(self):
        fake = FakeResponses(None)
        guard = CostGuard(self.path, 8.0)
        guard.ledger["used_cny_upper"] = 7.1
        guard.write()
        with self.assertRaises(BudgetExhausted):
            await guard.create(fake, instructions="test", input=[], tools=[], reasoning={},
                               max_output_tokens=100)
        resumed = CostGuard(self.path, 8.0, safety_margin_cny=0.5)
        await resumed.create(fake, instructions="test", input=[], tools=[], reasoning={},
                             max_output_tokens=100)
        self.assertEqual(fake.calls, 1)
        self.assertLess(resumed.ledger["used_cny_upper"], 7.5)
        self.assertEqual(resumed.ledger["requests"][-1]["safety_margin_cny"], 0.5)


if __name__ == "__main__":
    unittest.main()
