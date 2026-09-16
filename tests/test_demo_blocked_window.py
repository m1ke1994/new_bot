import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.app.browser.market import (
    NEXT_GOAL_SEARCH_TEXT,
    MarketNotAvailable,
    read_next_goal_odds,
)
from backend.app.demo.engine import (
    BlockedMatchSwitch,
    DemoBlockedWindow,
    DemoEngine,
    MissedSelectedTeamGoal,
    selected_team_scored_between,
)
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, ScoreboardSnapshot, Scorer
from backend.app.demo.strategy import StrategyConfig
from backend.app.demo.state import DemoStateStore


def snapshot(score1: int, score2: int) -> ScoreboardSnapshot:
    return ScoreboardSnapshot("TEAM 1", "TEAM 2", Score(score1, score2), "01:00", "1-й тайм")


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()


class QueueMatchBrowser:
    snapshots: list[ScoreboardSnapshot] = []

    def __init__(self, _page):
        pass

    async def snapshot(self):
        return self.snapshots.pop(0)


class EmptyLocator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        raise RuntimeError("no market groups")


class ReadOnlyPage:
    def locator(self, selector):
        if "game-search" in selector:
            raise AssertionError("DEMO market reader must not access or fill search input")
        return EmptyLocator()


class OneMatchLeagueBrowser:
    last_scan_stats = {"total": 1, "started": 0, "upcoming": 1}
    skipped_started = []
    skipped_excluded = []

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        pass

    async def scan(self):
        return [
            {
                "match_id": "match",
                "url": "https://example.test/match",
                "href": "/match",
                "team1": "TEAM 1",
                "team2": "TEAM 2",
                "time": "00:30",
                "period": "",
            }
        ]

    async def open_match(self, _selected_match):
        return {"url": "https://example.test/match"}


class DemoBlockedWindowTests(unittest.IsolatedAsyncioTestCase):
    def window(self) -> DemoBlockedWindow:
        return DemoBlockedWindow(
            initial_score=Score(2, 0),
            blocked_score_before=Score(2, 0),
            selected_side=Scorer.TEAM_2,
            selected_team="TEAM 2",
            step=3,
            stake=102,
            match_id="match",
        )

    def test_shared_selected_side_delta_helper(self):
        self.assertTrue(
            selected_team_scored_between(Score(2, 0), Score(4, 1), Scorer.TEAM_2)
        )
        self.assertFalse(
            selected_team_scored_between(Score(2, 0), Score(4, 0), Scorer.TEAM_2)
        )

    async def run_market_wait(self, snapshots, odds_side_effect):
        QueueMatchBrowser.snapshots = list(snapshots)
        window = self.window()
        reader = AsyncMock(side_effect=odds_side_effect)
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            state_store = DemoStateStore()
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.STATE", state_store),
                patch("backend.app.demo.engine.MatchBrowser", QueueMatchBrowser),
                patch("backend.app.demo.engine.read_next_goal_odds", reader),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._sleep_or_stop = AsyncMock()
                engine._publish_snapshot = AsyncMock()
                engine._status = AsyncMock()
                result = await engine._wait_for_odds(
                    snapshot(2, 0),
                    {"match_id": "match"},
                    demo_blocked_window=window,
                )
                logs = await repository.logs()
                state = await state_store.snapshot()
        return engine, window, reader, result, logs, state

    async def test_opponent_only_jump_updates_baseline_then_creates_fresh_market(self):
        fresh_odds = NextGoalOdds(1.9, 2.1, next_goal_number=5)
        engine, window, reader, result, logs, _ = await self.run_market_wait(
            [snapshot(2, 0), snapshot(4, 0), snapshot(4, 0)],
            [MarketNotAvailable("locked", status="MARKET_LOCKED"), fresh_odds],
        )

        current, odds = result
        self.assertEqual(current.score, Score(4, 0))
        self.assertIs(odds, fresh_odds)
        self.assertEqual(window.blocked_score_before, Score(4, 0))
        self.assertFalse(window.blocked_window_active)
        self.assertEqual(reader.await_args_list[-1].kwargs, {"read_only": True})
        self.assertIn("DEMO_BLOCKED_OPPONENT_SCORED", [item["event"] for item in logs])
        self.assertIn("DEMO_BLOCKED_MARKET_RECOVERED", [item["event"] for item in logs])
        self.assertEqual(engine.live_executor._states, {})

    async def test_unchanged_score_unlocks_same_step_and_stake(self):
        fresh_odds = NextGoalOdds(1.9, 2.1, next_goal_number=3)
        _, window, _, result, _, _ = await self.run_market_wait(
            [snapshot(2, 0), snapshot(2, 0), snapshot(2, 0)],
            [MarketNotAvailable("locked", status="ODDS_NOT_FOUND"), fresh_odds],
        )

        self.assertEqual(result[0].score, Score(2, 0))
        self.assertEqual(window.step, 3)
        self.assertEqual(window.stake, 102)
        self.assertEqual(window.blocked_score_before, Score(2, 0))

    async def test_canvas_odds_are_published_to_frontend_state(self):
        canvas_odds = NextGoalOdds(
            2.0,
            1.74,
            market="Следующий гол №1",
            next_goal_number=1,
            source="CANVAS_VISION",
            ocr_backend="RapidOCR",
            confidence=0.99,
        )
        _, _, _, result, _, state = await self.run_market_wait(
            [snapshot(0, 0), snapshot(0, 0)],
            [canvas_odds],
        )

        self.assertIs(result[1], canvas_odds)
        self.assertEqual(state["odds"]["team1"], 2.0)
        self.assertEqual(state["odds"]["team2"], 1.74)
        self.assertEqual(state["odds"]["source"], "CANVAS_VISION")
        self.assertEqual(state["market_reader"]["source"], "Canvas Vision / RapidOCR")
        self.assertEqual(state["market_reader"]["status"], "READY")

    async def test_initial_market_loading_is_not_a_demo_blocked_window(self):
        QueueMatchBrowser.snapshots = [snapshot(0, 0), snapshot(0, 0), snapshot(0, 0)]
        reader = AsyncMock(
            side_effect=[
                MarketNotAvailable("loading", status="ELEMENT_NOT_READY"),
                NextGoalOdds(1.8, 2.1, next_goal_number=1),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.MatchBrowser", QueueMatchBrowser),
                patch("backend.app.demo.engine.read_next_goal_odds", reader),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._sleep_or_stop = AsyncMock()
                engine._publish_snapshot = AsyncMock()
                engine._status = AsyncMock()
                result = await engine._wait_for_odds(
                    snapshot(0, 0),
                    {"match_id": "match"},
                )
                logs = await repository.logs()

        self.assertIsNotNone(result)
        self.assertNotIn(
            "DEMO_BLOCKED_WINDOW_STARTED",
            [item["event"] for item in logs],
        )

    async def test_selected_team_goal_during_blocked_window_raises_missed(self):
        QueueMatchBrowser.snapshots = [snapshot(2, 0), snapshot(3, 1)]
        window = self.window()
        reader = AsyncMock(
            side_effect=MarketNotAvailable("locked", status="MARKET_LOCKED")
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.MatchBrowser", QueueMatchBrowser),
                patch("backend.app.demo.engine.read_next_goal_odds", reader),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._sleep_or_stop = AsyncMock()
                engine._publish_snapshot = AsyncMock()
                engine._status = AsyncMock()
                with self.assertRaises(MissedSelectedTeamGoal) as caught:
                    await engine._wait_for_odds(
                        snapshot(2, 0),
                        {"match_id": "match"},
                        demo_blocked_window=window,
                    )

        self.assertEqual(caught.exception.snapshot.score, Score(3, 1))
        self.assertTrue(window.blocked_window_active)

    async def test_multiple_opponent_polls_then_selected_goal_switches_only_at_last_poll(self):
        QueueMatchBrowser.snapshots = [
            snapshot(2, 0),
            snapshot(3, 0),
            snapshot(4, 0),
            snapshot(4, 1),
        ]
        window = self.window()
        reader = AsyncMock(
            side_effect=MarketNotAvailable("locked", status="MARKET_LOCKED")
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.MatchBrowser", QueueMatchBrowser),
                patch("backend.app.demo.engine.read_next_goal_odds", reader),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._sleep_or_stop = AsyncMock()
                engine._publish_snapshot = AsyncMock()
                engine._status = AsyncMock()
                with self.assertRaises(MissedSelectedTeamGoal):
                    await engine._wait_for_odds(
                        snapshot(2, 0),
                        {"match_id": "match"},
                        demo_blocked_window=window,
                    )

        self.assertEqual(window.blocked_score_before, Score(4, 0))
        self.assertEqual(reader.await_count, 3)

    async def test_demo_missed_result_preserves_progress_and_blacklists_match(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            sequence = await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                cumulative_losses="65.00",
            )
            budget_before = await repository.get_budget()
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "stakes": [20, 45, 102],
                        "max_steps": 3,
                        "blocked_events_switch_enabled": True,
                    }
                )
                result = await engine._finish_demo_missed_selected_team_goal(
                    window=self.window(),
                    selected_match={"match_id": "match"},
                    cycle_id=sequence["sequence_id"],
                    snapshot=snapshot(3, 1),
                )
                history = await repository.history(mode="DEMO")
                saved_sequence = await repository.get_sequence()
                budget_after = await repository.get_budget()

        self.assertIsInstance(result, BlockedMatchSwitch)
        self.assertEqual(history[0]["result"], "MISSED_SELECTED_TEAM_GOAL")
        self.assertEqual(history[0]["placement_signal"], "DEMO_MARKET_BLOCKED")
        self.assertEqual(history[0]["budget_change"], 0)
        self.assertEqual(saved_sequence["current_step"], 3)
        self.assertEqual(saved_sequence["cumulative_losses"], "65.00")
        self.assertIn("match", saved_sequence["blocked_match_ids"])
        self.assertEqual(budget_after, budget_before)
        self.assertEqual(engine.live_executor._states, {})

    async def test_demo_strategy_switches_after_selected_goal_before_next_active_bet(self):
        zero = snapshot(0, 0)
        opponent_goal = snapshot(1, 0)
        selected_goal = snapshot(1, 1)
        coefficient_locator = Mock()
        coefficient_locator.click = AsyncMock()
        odds = NextGoalOdds(
            1.8,
            2.1,
            next_goal_number=1,
            team1_locator=Mock(),
            team2_locator=coefficient_locator,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            budget_before = await repository.get_budget()
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", OneMatchLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "stakes": [20, 45, 102],
                        "max_steps": 3,
                        "blocked_events_switch_enabled": True,
                    }
                )
                engine._sleep_or_stop = AsyncMock()
                engine.live_executor.prepare = AsyncMock()
                engine._wait_for_initial_zero_score = AsyncMock(return_value=zero)
                engine._wait_for_odds = AsyncMock(
                    side_effect=[
                        (zero, odds),
                        MissedSelectedTeamGoal(selected_goal),
                    ]
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[zero, zero, opponent_goal]
                )
                engine._wait_for_goal = AsyncMock(
                    return_value=(opponent_goal, Scorer.TEAM_1)
                )

                await engine._process_next_match(object())

                history = await repository.history(mode="DEMO")
                saved_sequence = await repository.get_sequence()
                budget_after = await repository.get_budget()

        self.assertEqual(
            [item["result"] for item in history],
            ["LOSE", "MISSED_SELECTED_TEAM_GOAL"],
        )
        self.assertEqual([item["step"] for item in history], [1, 2])
        self.assertEqual(history[1]["amount"], 45)
        self.assertEqual(history[1]["budget_change"], 0)
        self.assertEqual(saved_sequence["current_step"], 2)
        self.assertEqual(saved_sequence["status"], "WAITING_NEXT_MATCH")
        self.assertIn("match", saved_sequence["blocked_match_ids"])
        self.assertEqual(
            budget_after["current_budget"],
            budget_before["current_budget"] - 20,
        )
        engine.live_executor.prepare.assert_not_awaited()
        coefficient_locator.click.assert_not_awaited()

    async def test_opponent_only_blocked_window_creates_step_three_at_updated_score(self):
        scores = {
            "zero": snapshot(0, 0),
            "one": snapshot(1, 0),
            "two": snapshot(2, 0),
            "four": snapshot(4, 0),
        }
        odds_by_goal = {
            number: NextGoalOdds(1.8, 2.1, next_goal_number=number)
            for number in (1, 2, 5)
        }

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", OneMatchLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "stakes": [20, 45, 102],
                        "max_steps": 3,
                        "blocked_events_switch_enabled": True,
                    }
                )
                engine._sleep_or_stop = AsyncMock()
                engine.live_executor.prepare = AsyncMock()
                engine._wait_for_initial_zero_score = AsyncMock(
                    return_value=scores["zero"]
                )
                market_calls = 0

                async def wait_for_market(current, _selected_match, **kwargs):
                    nonlocal market_calls
                    market_calls += 1
                    window = kwargs.get("demo_blocked_window")
                    if market_calls == 1:
                        self.assertIsNone(window)
                        return scores["zero"], odds_by_goal[1]
                    if market_calls == 2:
                        self.assertEqual(window.step, 2)
                        return scores["one"], odds_by_goal[2]
                    self.assertEqual(window.step, 3)
                    window.blocked_window_active = True
                    await engine._observe_demo_blocked_score(window, scores["four"])
                    window.blocked_window_active = False
                    return scores["four"], odds_by_goal[5]

                engine._wait_for_odds = wait_for_market
                engine._read_fresh_score = AsyncMock(
                    side_effect=[
                        scores["zero"],
                        scores["zero"],
                        scores["one"],
                        scores["one"],
                        scores["one"],
                        scores["two"],
                        scores["four"],
                        scores["four"],
                    ]
                )
                engine._wait_for_goal = AsyncMock(
                    side_effect=[
                        (scores["one"], Scorer.TEAM_1),
                        (scores["two"], Scorer.TEAM_1),
                        None,
                    ]
                )

                await engine._process_next_match(object())
                history = await repository.history(mode="DEMO")
                sequence = await repository.get_sequence()

        self.assertEqual([item["result"] for item in history], ["LOSE", "LOSE", "ACTIVE"])
        self.assertEqual(history[-1]["step"], 3)
        self.assertEqual(history[-1]["amount"], 102)
        self.assertEqual(history[-1]["score_before"], "4:0")
        self.assertEqual(history[-1]["next_goal_number"], 5)
        self.assertEqual(sequence["current_step"], 3)
        self.assertEqual(sequence["blocked_match_ids"], [])
        engine.live_executor.prepare.assert_not_awaited()

    async def test_read_only_market_reader_prepares_filter_without_betting(self):
        page = ReadOnlyPage()
        with patch(
            "backend.app.browser.market._prepare_market_search",
            new_callable=AsyncMock,
        ) as prepare_search:
            with self.assertRaises(MarketNotAvailable):
                await read_next_goal_odds(
                    page,
                    "TEAM 1",
                    "TEAM 2",
                    2,
                    0,
                    read_only=True,
                )

        prepare_search.assert_awaited_once_with(
            page,
            NEXT_GOAL_SEARCH_TEXT,
            None,
        )


if __name__ == "__main__":
    unittest.main()
