import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, ScoreboardSnapshot, Scorer
from backend.app.demo.strategy import StrategyConfig


MATCH = {
    "match_id": "borussia-lille",
    "url": "https://example.test/borussia-lille",
    "href": "/borussia-lille",
    "team1": "Боруссия Мёнхенгладбах",
    "team2": "Лилль",
    "time": "00:30",
    "period": "",
}


def snapshot(score1: int, score2: int) -> ScoreboardSnapshot:
    return ScoreboardSnapshot(
        MATCH["team1"],
        MATCH["team2"],
        Score(score1, score2),
        "01:00",
        "1-й тайм",
    )


def odds(goal: int, team1: float = 1.984, team2: float = 1.784) -> NextGoalOdds:
    return NextGoalOdds(
        team1,
        team2,
        market=f"Следующий гол №{goal}",
        next_goal_number=goal,
    )


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class FakeLeagueBrowser:
    scan_calls = 0
    open_match_calls = 0
    last_scan_stats = {"total": 2, "started": 0, "upcoming": 2}
    skipped_started = []

    def __init__(self, _page, *, exclude_teams_enabled=True):
        self.exclude_teams_enabled = exclude_teams_enabled

    async def open(self):
        pass

    async def scan(self):
        type(self).scan_calls += 1
        return [MATCH, {**MATCH, "match_id": "basel-roma", "team1": "Базель", "team2": "Рома"}]

    async def open_match(self, selected):
        type(self).open_match_calls += 1
        if selected["match_id"] != MATCH["match_id"]:
            raise AssertionError("Бот переключился на другой матч")
        return {"url": selected["url"]}


class DemoSeriesLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_disabled_filter_allows_series_start(
        self,
        *,
        mode: str,
        initial: ScoreboardSnapshot,
        initial_odds: NextGoalOdds,
        exclude_teams_enabled: bool,
        min_initial_odds_enabled: bool,
    ):
        class SeriesStartReached(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = mode
                engine._config = StrategyConfig.from_payload(
                    {
                        "exclude_teams_enabled": exclude_teams_enabled,
                        "min_initial_odds_enabled": min_initial_odds_enabled,
                    }
                )
                engine._sleep_or_stop = AsyncMock()
                engine._wait_for_initial_zero_score = AsyncMock(
                    return_value=initial
                )
                engine._wait_for_odds = AsyncMock(
                    return_value=(initial, initial_odds)
                )
                repository.save_sequence = AsyncMock(
                    side_effect=SeriesStartReached
                )

                with self.assertRaises(SeriesStartReached):
                    await engine._process_next_match(object())

                logs = await repository.logs()

        repository.save_sequence.assert_awaited_once_with(
            status="PENDING" if mode == "LIVE" else "ACTIVE",
            current_match_id=MATCH["match_id"],
            selected_team=initial.team1,
        )
        return logs

    async def test_disabled_minimum_odds_filter_allows_low_odds_in_both_modes(self):
        initial = snapshot(0, 0)
        for mode in ("DEMO", "LIVE"):
            with self.subTest(mode=mode):
                logs = await self._assert_disabled_filter_allows_series_start(
                    mode=mode,
                    initial=initial,
                    initial_odds=NextGoalOdds(team1=1.80, team2=1.70),
                    exclude_teams_enabled=True,
                    min_initial_odds_enabled=False,
                )
                self.assertNotIn(
                    "MATCH_SKIPPED_LOW_INITIAL_ODDS",
                    [item["event"] for item in logs],
                )

    async def test_disabled_team_filter_allows_chelsea_in_both_modes(self):
        initial = ScoreboardSnapshot(
            MATCH["team1"],
            "Chelsea",
            Score(0, 0),
            "00:00",
            "",
        )
        for mode in ("DEMO", "LIVE"):
            with self.subTest(mode=mode):
                logs = await self._assert_disabled_filter_allows_series_start(
                    mode=mode,
                    initial=initial,
                    initial_odds=NextGoalOdds(team1=1.95, team2=1.80),
                    exclude_teams_enabled=False,
                    min_initial_odds_enabled=True,
                )
                self.assertNotIn(
                    "MATCH_SKIPPED_EXCLUDED_TEAM",
                    [item["event"] for item in logs],
                )

    async def test_low_initial_odds_skip_series_in_demo_and_live(self):
        for mode in ("DEMO", "LIVE"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                repository = DemoRepository(Path(directory))
                with (
                    patch("backend.app.demo.engine.REPOSITORY", repository),
                    patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
                ):
                    engine = DemoEngine(FakeManager())
                    engine._mode = mode
                    engine._sleep_or_stop = AsyncMock()
                    initial = snapshot(0, 0)
                    engine._wait_for_initial_zero_score = AsyncMock(
                        return_value=initial
                    )
                    engine._wait_for_odds = AsyncMock(
                        return_value=(
                            initial,
                            NextGoalOdds(team1=1.92, team2=1.90),
                        )
                    )
                    engine._read_fresh_score = AsyncMock()

                    await engine._process_next_match(object())

                    history = await repository.history(mode=mode)
                    sequence = await repository.get_sequence()
                    logs = await repository.logs()

                self.assertEqual(history, [])
                self.assertEqual(sequence["status"], "WAITING_FOR_MATCH")
                self.assertIsNone(sequence["current_match_id"])
                self.assertIsNone(sequence["selected_team"])
                self.assertIsNone(engine._current_series)
                self.assertIsNone(engine._pending_live_bet)
                self.assertFalse(engine._read_fresh_score.await_count)
                self.assertIn(
                    "MATCH_SKIPPED_LOW_INITIAL_ODDS",
                    [item["event"] for item in logs],
                )

    async def test_scoreboard_team_filter_is_final_safety_check_in_both_modes(self):
        excluded = ScoreboardSnapshot(
            "Chelsea",
            "Lille",
            Score(0, 0),
            "00:00",
            "",
        )
        for mode in ("DEMO", "LIVE"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                repository = DemoRepository(Path(directory))
                with (
                    patch("backend.app.demo.engine.REPOSITORY", repository),
                    patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
                ):
                    engine = DemoEngine(FakeManager())
                    engine._mode = mode
                    engine._sleep_or_stop = AsyncMock()
                    engine._wait_for_initial_zero_score = AsyncMock(
                        return_value=excluded
                    )
                    engine._wait_for_odds = AsyncMock()

                    await engine._process_next_match(object())

                    history = await repository.history(mode=mode)
                    sequence = await repository.get_sequence()
                    logs = await repository.logs()

                self.assertEqual(history, [])
                self.assertEqual(sequence["status"], "WAITING_FOR_MATCH")
                self.assertIsNone(engine._current_series)
                engine._wait_for_odds.assert_not_awaited()
                self.assertIn(
                    "MATCH_SKIPPED_EXCLUDED_TEAM",
                    [item["event"] for item in logs],
                )

    async def _run_two_step_series(
        self,
        *,
        score_after_loss: ScoreboardSnapshot,
        final_score: ScoreboardSnapshot,
    ):
        FakeLeagueBrowser.scan_calls = 0
        FakeLeagueBrowser.open_match_calls = 0
        initial = snapshot(0, 0)
        lost = snapshot(0, 1)

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.LeagueBrowser", FakeLeagueBrowser),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "initial_stake": 211,
                        "progression_multiplier": 2.2,
                        "max_steps": 2,
                        "stakes": [211, 464],
                    }
                )
                engine._wait_for_initial_zero_score = AsyncMock(return_value=initial)
                engine._wait_for_odds = AsyncMock(
                    side_effect=[
                        (initial, odds(1)),
                        (
                            score_after_loss,
                            odds(
                                score_after_loss.score.team1
                                + score_after_loss.score.team2
                                + 1,
                                team1=1.89,
                                team2=2.05,
                            ),
                        ),
                    ]
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[initial, score_after_loss, score_after_loss]
                )
                engine._wait_for_goal = AsyncMock(
                    side_effect=[
                        (lost, Scorer.TEAM_2),
                        (final_score, Scorer.TEAM_1),
                    ]
                )

                await engine._process_next_match(object())

                history = await repository.history(mode="DEMO")
                sequence = await repository.get_sequence()

        return history, sequence

    async def test_lose_continues_same_match_and_team_below_entry_threshold(self):
        history, sequence = await self._run_two_step_series(
            score_after_loss=snapshot(0, 1),
            final_score=snapshot(1, 1),
        )

        self.assertEqual(FakeLeagueBrowser.scan_calls, 1)
        self.assertEqual(FakeLeagueBrowser.open_match_calls, 1)
        self.assertEqual([item["result"] for item in history], ["LOSE", "WIN"])
        self.assertEqual([item["match_id"] for item in history], [MATCH["match_id"]] * 2)
        self.assertEqual([item["selected_team"] for item in history], [MATCH["team1"]] * 2)
        self.assertEqual([item["step"] for item in history], [1, 2])
        self.assertEqual([item["amount"] for item in history], [211.0, 464.0])
        self.assertEqual([item["odds"] for item in history], [1.984, 1.89])
        self.assertEqual([item["next_goal_number"] for item in history], [1, 2])
        self.assertEqual(sequence["status"], "WAITING_FOR_MATCH")
        self.assertEqual(sequence["current_step"], 1)

    async def test_score_change_without_bet_keeps_match_team_and_step(self):
        history, _sequence = await self._run_two_step_series(
            score_after_loss=snapshot(0, 2),
            final_score=snapshot(1, 2),
        )

        self.assertEqual(FakeLeagueBrowser.scan_calls, 1)
        self.assertEqual([item["match_id"] for item in history], [MATCH["match_id"]] * 2)
        self.assertEqual([item["selected_team"] for item in history], [MATCH["team1"]] * 2)
        self.assertEqual([item["step"] for item in history], [1, 2])
        self.assertEqual(history[1]["score_before"], "0:2")
        self.assertEqual(history[1]["next_goal_number"], 3)


if __name__ == "__main__":
    unittest.main()
