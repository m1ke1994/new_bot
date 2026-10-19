import asyncio
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.app.virtual_forks import (
    ForkStore, VirtualForkObserver, cash, clock_seconds, finished,
    split_virtual_stakes,
)


class VirtualForkCalculationTests(unittest.TestCase):
    def test_two_team_conditional_hedge(self):
        p = split_virtual_stakes(250, 2.30, 2.20)
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p["stake1"] + p["stake2"], 250)
        self.assertLess(p["inverse_sum"], 1)
        self.assertGreater(p["minimum_conditional_profit"], 0)
        self.assertEqual(p["no_goal_loss"], -250)

    def test_no_signal_when_overround_is_positive(self):
        self.assertIsNone(split_virtual_stakes(500, 1.80, 2.10))
        self.assertIsNone(split_virtual_stakes(100, 2, 2))

    def test_no_signal_if_rounding_eliminates_profit(self):
        self.assertIsNone(split_virtual_stakes(Decimal("0.02"), 2.001, 2.001))

    def test_budget_split_does_not_exceed_allocated_total(self):
        p = split_virtual_stakes("5000.00", 2.5, 2.8)
        self.assertEqual(cash(p["stake1"]) + cash(p["stake2"]), Decimal("5000.00"))

    def test_window_boundaries(self):
        self.assertEqual(clock_seconds("05:00"), 300)
        self.assertEqual(clock_seconds("05:30"), 330)
        self.assertEqual(clock_seconds("05:31"), 331)
        self.assertIsNone(clock_seconds("Второй тайм"))
        self.assertIsNone(clock_seconds("23:89"))

    def test_no_goal_requires_finished_state(self):
        self.assertTrue(finished("Игра завершена"))
        self.assertFalse(finished("2-й тайм"))
        self.assertFalse(finished("06:00"))

    def test_history_persisted_separately(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "forks.sqlite3"
            store = ForkStore(path)
            item = dict(
                id="pair-1", session_id="session", match_id="match-1",
                created_at="2026-10-19T15:00:00Z", status="PENDING",
                stake1=60, stake2=40,
            )
            store.upsert(item)
            self.assertEqual(len(ForkStore(path).history()), 1)
            item["status"] = "SETTLED"
            store.upsert(item)
            self.assertEqual(len(store.history()), 1)
            self.assertEqual(store.history()[0]["status"], "SETTLED")


class VirtualForkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_is_virtual_only(self):
        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            async def mocked_worker():
                await observer._stop_event.wait()
            with patch.object(observer, "_run", side_effect=mocked_worker):
                with patch("backend.app.virtual_forks.ENGINE") as engine:
                    # task is an @property on DemoEngine, not a method.
                    engine.task = None
                    state = await observer.start(5000, 10)
                    self.assertTrue(state["running"])
                    self.assertEqual(state["balance"], 5000)
                    self.assertEqual(state["allocated_percent"], 10)
                    self.assertTrue((await observer.stop())["phase"] == "STOPPED")

    async def test_start_rejects_concurrent_main_engine_task(self):
        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            other_task = asyncio.create_task(asyncio.Event().wait())
            try:
                with patch("backend.app.virtual_forks.ENGINE") as engine:
                    engine.task = other_task
                    with self.assertRaisesRegex(RuntimeError, "основную стратегию"):
                        await observer.start(5000, 10)
                self.assertFalse(observer.running)
            finally:
                other_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await other_task

    async def test_unresolved_pair_is_not_falsely_settled(self):
        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            observer._session = "test"
            observer._balance = Decimal("100")
            observer._initial = Decimal("200")
            observer._position = {
                "id": "x", "session_id": "test", "match_id": "1",
                "created_at": "2026-10-19T15:00:00Z",
                "status": "PENDING", "match": "А — Б",
            }
            await observer._review("Неясный результат")
            self.assertEqual(observer.store.history()[0]["status"], "NEEDS_REVIEW")
            self.assertEqual(observer._balance, Decimal("100"))


class VirtualForkRoutesTests(unittest.TestCase):
    def test_api_routes_registered(self):
        from backend.app.main import app
        routes = {(r.path, method) for r in app.routes for method in getattr(r, "methods", set())}
        for path, method in (
            ("/api/forks/start", "POST"), ("/api/forks/stop", "POST"),
            ("/api/forks/state", "GET"), ("/api/forks/history", "GET"),
            ("/api/forks/logs", "GET"),
        ):
            self.assertIn((path, method), routes)


if __name__ == "__main__":
    unittest.main()
