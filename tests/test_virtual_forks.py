import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.virtual_forks import (
    ForkStore, VirtualForkObserver, cash, clock_seconds, finished,
    split_virtual_stakes, select_nearest_unstarted, MatchStartGate,
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


class ForkPrematchSelectionTests(unittest.TestCase):
    def test_selects_nearest_eligible_countdown_not_active(self):
        cards = [
            dict(match_id="live", team1="A", team2="B", score="0:0",
                 time="05:47", time_seconds=347, period="2-й тайм",
                 is_upcoming=False, finished=False),
            dict(match_id="later", team1="C", team2="D", score="0:0",
                 time="01:40", time_seconds=100, period="",
                 is_upcoming=True, finished=False),
            dict(match_id="nearest", team1="E", team2="F", score="0:0",
                 time="00:45", time_seconds=45, period="",
                 is_upcoming=True, finished=False),
            dict(match_id="done", team1="G", team2="H", score="0:0",
                 time="00:15", time_seconds=15, period="",
                 is_upcoming=True, finished=True),
        ]
        self.assertEqual(select_nearest_unstarted(cards)["match_id"], "nearest")
        self.assertEqual(
            select_nearest_unstarted(cards, {"nearest"})["match_id"], "later",
        )
        self.assertIsNone(select_nearest_unstarted(cards, {"nearest", "later"}))

    def test_score_and_missing_countdown_are_not_prematch(self):
        self.assertIsNone(select_nearest_unstarted([
            dict(match_id="a", is_upcoming=True, time_seconds=None, score="0:0"),
            dict(match_id="b", is_upcoming=True, time_seconds=45, score="1:0"),
            dict(match_id="c", is_upcoming=True, time_seconds=0, score="0:0"),
            dict(match_id="d", is_upcoming=True, time_seconds=30,
                 score="0:0", period="1-й тайм"),
        ]))

    @staticmethod
    def snap(timer, period="", goals1=0, goals2=0):
        score = SimpleNamespace(team1=goals1, team2=goals2)
        return SimpleNamespace(timer=timer, period=period, score=score)

    def test_pregame_5_minute_timer_is_not_live_window(self):
        gate = MatchStartGate()
        self.assertFalse(gate.observe(self.snap("05:30")))
        self.assertFalse(gate.observe(self.snap("05:15")))
        self.assertFalse(gate.observe(self.snap("05:00")))
        self.assertFalse(gate.observe(self.snap("00:00")))
        self.assertTrue(gate.observe(self.snap("00:01")))

    def test_live_period_or_goal_confirms_start(self):
        gate = MatchStartGate()
        self.assertFalse(gate.observe(self.snap("00:20", "Не начался")))
        self.assertTrue(gate.observe(self.snap("00:01", "1-й тайм")))
        self.assertTrue(gate.started)
        self.assertTrue(MatchStartGate().observe(self.snap("00:15", "", goals1=1)))


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

    async def test_recovers_closed_browser_page_without_stale_reuse(self):
        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))

            class FakePage:
                def __init__(self):
                    self.closed = False
                def is_closed(self):
                    return self.closed

            first, second = FakePage(), FakePage()

            async def scan(page):
                if page is first:
                    page.closed = True
                    raise RuntimeError("Target page, context or browser has been closed")
                observer._stop_event.set()

            with patch("backend.app.virtual_forks.ENGINE") as engine, \
                 patch("backend.app.virtual_forks.BROWSER_MANAGER") as manager, \
                 patch("backend.app.virtual_forks.authorize", new_callable=AsyncMock) as auth:
                engine.task = None
                manager.generation = 1
                manager.ensure_page = AsyncMock(side_effect=[first, second])
                auth.return_value = {"ok": True, "status": "AUTHORIZED"}
                with patch.object(observer, "_scan_one", new_callable=AsyncMock) as scan_mock, \
                     patch.object(observer, "pause", new_callable=AsyncMock):
                    scan_mock.side_effect = scan
                    await observer.start(5000, 10)
                    await asyncio.wait_for(observer._task, timeout=3)
                    self.assertEqual(manager.ensure_page.await_count, 2)
                    self.assertEqual(auth.await_count, 2)
                    self.assertEqual(scan_mock.await_count, 2)
            messages = observer.store.logs()
            self.assertTrue(any(item["event"] == "BROWSER_PAGE_LOST" for item in messages))

    async def test_empty_scan_diagnostic_does_not_reload_ready_league(self):
        from urllib.parse import urlparse
        from backend.app.demo.config import CONFIG

        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            observer._session = "scan-test"

            class FakePage:
                url = "https://test.local" + urlparse(CONFIG.league_url).path

            with patch("backend.app.virtual_forks.LeagueBrowser") as league_class:
                league = league_class.return_value
                league.scan = AsyncMock(return_value=[])
                league.open = AsyncMock()
                league.last_scan_stats = {
                    "total": 2, "upcoming": 1, "started": 0,
                    "finished": 0, "excluded": 0,
                    "initial_live_skipped": 0, "unclassified": 1,
                }
                league.skipped_unclassified = [{
                    "team1": "А", "team2": "Б", "time": "",
                    "period": "", "reason": "countdown_or_period_not_ready",
                }]
                await observer._scan_one(FakePage())
                league.open.assert_not_awaited()
                self.assertEqual(observer.snapshot()["phase"], "WAITING_CARDS")
                self.assertEqual(observer.snapshot()["scanner"]["unclassified"], 1)
                self.assertTrue(any(
                    "непрочитанных=1" in line["message"]
                    for line in observer.store.logs()
                ))

    async def test_waits_for_live_before_reading_odds_and_skips_late(self):
        from urllib.parse import urlparse
        from backend.app.demo.config import CONFIG

        class Score:
            team1 = 0
            team2 = 0
            def text(self):
                return "0:0"

        def snap(timer, period):
            return SimpleNamespace(
                timer=timer, period=period, score=Score(),
                team1="Команда А", team2="Команда Б",
            )

        match = dict(
            match_id="upcoming-1", href="/match/upcoming-1",
            team1="Команда А", team2="Команда Б",
            time="00:30", time_seconds=30, period="", score="0:0",
            is_upcoming=True, finished=False,
        )

        class FakePage:
            url = "https://example.test" + urlparse(CONFIG.league_url).path

        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            observer._session = "timing-test"
            with patch("backend.app.virtual_forks.LeagueBrowser") as league_cls, \
                 patch("backend.app.virtual_forks.read_scoreboard", new_callable=AsyncMock) as scores, \
                 patch("backend.app.virtual_forks.read_next_goal_odds", new_callable=AsyncMock) as odds, \
                 patch.object(observer, "pause", new_callable=AsyncMock):
                league = league_cls.return_value
                league.scan = AsyncMock(return_value=[match])
                league.open = AsyncMock()
                league.revalidate_upcoming = AsyncMock(return_value=match)
                league.open_match = AsyncMock()
                league.last_scan_stats = {
                    "total": 1, "upcoming": 1, "started": 0,
                    "finished": 0, "excluded": 0,
                    "initial_live_skipped": 0, "unclassified": 0,
                }
                # 05:15 is still countdown; it must NEVER trigger a wager.
                # When live is finally confirmed, we are already past 05:30.
                scores.side_effect = [
                    snap("05:15", ""), snap("05:00", ""),
                    snap("00:01", "1-й тайм"),
                    snap("05:31", "2-й тайм"),
                ]
                await observer._scan_one(FakePage())
                league.open.assert_not_awaited()
                league.open_match.assert_awaited_once()
                odds.assert_not_awaited()
                self.assertIn("upcoming-1", observer._completed_match_ids)
                self.assertTrue(any(
                    line["event"] == "MATCH_LIVE_STARTED"
                    for line in observer.store.logs()
                ))
                self.assertTrue(any(
                    line["event"] == "WINDOW_CLOSED"
                    for line in observer.store.logs()
                ))

    async def test_virtual_pair_is_only_created_after_confirmed_live_at_0500(self):
        from urllib.parse import urlparse
        from backend.app.demo.config import CONFIG

        class Score:
            team1 = 0
            team2 = 0
            def text(self):
                return "0:0"

        def snap(timer, period):
            return SimpleNamespace(
                timer=timer, period=period, score=Score(),
                team1="А", team2="Б",
            )

        match = dict(
            match_id="future-m2", href="/match/future-m2",
            team1="А", team2="Б", time="00:15", time_seconds=15,
            period="", score="0:0", is_upcoming=True, finished=False,
        )

        class FakePage:
            url = "https://example.test" + urlparse(CONFIG.league_url).path

        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            observer._session = "positive-test"
            observer._balance = Decimal("5000")
            observer._initial = Decimal("5000")
            observer._allocation_pct = Decimal("10")
            with patch("backend.app.virtual_forks.LeagueBrowser") as cls, \
                 patch("backend.app.virtual_forks.read_scoreboard", new_callable=AsyncMock) as scores, \
                 patch("backend.app.virtual_forks.read_next_goal_odds", new_callable=AsyncMock) as odds, \
                 patch.object(observer, "pause", new_callable=AsyncMock), \
                 patch.object(observer, "_settle", new_callable=AsyncMock) as settle:
                league = cls.return_value
                league.scan = AsyncMock(return_value=[match])
                league.revalidate_upcoming = AsyncMock(return_value=match)
                league.open = AsyncMock()
                league.open_match = AsyncMock()
                league.last_scan_stats = {
                    "total": 1, "upcoming": 1, "started": 0, "finished": 0,
                    "excluded": 0, "unclassified": 0, "initial_live_skipped": 0,
                }
                scores.side_effect = [
                    snap("05:10", ""),  # PREMATCH countdown
                    snap("04:59", ""),  # PREMATCH countdown
                    snap("00:01", "1-й тайм"),  # confirmed LIVE
                    snap("04:59", "2-й тайм"),  # elapsed LIVE before window
                    snap("05:00", "2-й тайм"),  # first permitted signal
                    snap("05:01", "2-й тайм"),  # revalidate score/time
                ]
                odds.return_value = SimpleNamespace(
                    team1=2.2, team2=2.2, next_goal_number=1,
                    locked_sides=(), recent_locked_sides=(), source="MOCK_CANVAS",
                )
                await observer._scan_one(FakePage())
                odds.assert_awaited_once()
                settle.assert_awaited_once()
                self.assertEqual(observer._position["timer"], "05:01")
                self.assertEqual(observer._position["status"], "PENDING")
                self.assertEqual(observer._state["signals"], 1)
                self.assertEqual(len(observer.store.history()), 1)
                self.assertEqual(observer._state["balance"], 4500.0)

    async def test_revalidation_rejects_match_started_before_open(self):
        from urllib.parse import urlparse
        from backend.app.demo.config import CONFIG

        match = dict(
            match_id="m1", href="/match/m1", team1="А", team2="Б",
            time="00:30", time_seconds=30, period="",
            is_upcoming=True, score="0:0", finished=False,
        )

        class FakePage:
            url = "https://example.test" + urlparse(CONFIG.league_url).path

        with TemporaryDirectory() as tmp:
            observer = VirtualForkObserver(ForkStore(Path(tmp) / "forks.sqlite3"))
            observer._session = "strict-test"
            with patch("backend.app.virtual_forks.LeagueBrowser") as cls:
                league = cls.return_value
                league.scan = AsyncMock(return_value=[match])
                league.last_scan_stats = {"total": 1, "upcoming": 1}
                league.open = AsyncMock()
                league.open_match = AsyncMock()
                league.revalidate_upcoming = AsyncMock(return_value={
                    **match, "is_upcoming": False, "period": "1-й тайм",
                })
                await observer._scan_one(FakePage())
                league.open_match.assert_not_awaited()
                self.assertTrue(any(
                    line["event"] == "MATCH_STARTED_BEFORE_ENTRY"
                    for line in observer.store.logs()
                ))

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
