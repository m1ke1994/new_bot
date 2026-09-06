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

    def __init__(self, _page):
        pass

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
                                + 1
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

    async def test_lose_continues_same_match_and_team_until_win(self):
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
