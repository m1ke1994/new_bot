import unittest
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import (
    DEMO_ACCEPTANCE_CONFIRMATIONS,
    DemoBlockedWindow,
    DemoEngine,
    MissedSelectedTeamGoal,
)
from backend.app.demo.models import NextGoalOdds, Score, ScoreboardSnapshot, Scorer


def snapshot(score1: int, score2: int) -> ScoreboardSnapshot:
    return ScoreboardSnapshot(
        "TEAM 1",
        "TEAM 2",
        Score(score1, score2),
        "01:00",
        "1-й тайм",
    )


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass


class DemoAcceptanceGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = DemoEngine(FakeManager())
        self.engine._mode = "DEMO"
        self.engine._sleep_or_stop = AsyncMock()
        self.window = DemoBlockedWindow(
            initial_score=Score(3, 0),
            blocked_score_before=Score(3, 0),
            selected_side=Scorer.TEAM_2,
            selected_team="TEAM 2",
            step=4,
            stake=280,
            match_id="match",
            started_after_settlement=True,
        )
        self.odds = NextGoalOdds(
            1.61,
            2.205,
            next_goal_number=4,
            source="CANVAS_2D",
        )
        self.selected_match = {
            "match_id": "match",
            "team1": "TEAM 1",
            "team2": "TEAM 2",
        }

    async def test_requires_three_unlocked_confirmations_before_acceptance(self):
        baseline = snapshot(3, 0)
        self.engine._read_fresh_score = AsyncMock(
            side_effect=[baseline, baseline, baseline, baseline]
        )
        self.engine._demo_prebet_canvas_is_blocked = AsyncMock(return_value=False)

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            accepted, final_snapshot = await self.engine._confirm_demo_bet_acceptance(
                page=object(),
                browser=object(),
                selected_match=self.selected_match,
                current_odds=self.odds,
                window=self.window,
                snapshot=baseline,
            )

        self.assertTrue(accepted)
        self.assertEqual(final_snapshot.score, Score(3, 0))
        self.assertEqual(
            self.engine._demo_prebet_canvas_is_blocked.await_count,
            DEMO_ACCEPTANCE_CONFIRMATIONS,
        )
        self.assertEqual(self.engine._sleep_or_stop.await_count, 2)
        events = [call.args[0] for call in log.await_args_list]
        self.assertEqual(events.count("DEMO_OPEN_CONFIRMATION"), 3)
        self.assertIn("DEMO_BET_ACCEPTANCE_CONFIRMED", events)

    async def test_lock_after_one_open_sample_resets_acceptance(self):
        baseline = snapshot(3, 0)
        self.engine._read_fresh_score = AsyncMock(
            side_effect=[baseline, baseline]
        )
        self.engine._demo_prebet_canvas_is_blocked = AsyncMock(
            side_effect=[False, True]
        )

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            accepted, final_snapshot = await self.engine._confirm_demo_bet_acceptance(
                page=object(),
                browser=object(),
                selected_match=self.selected_match,
                current_odds=self.odds,
                window=self.window,
                snapshot=baseline,
            )

        self.assertFalse(accepted)
        self.assertEqual(final_snapshot.score, Score(3, 0))
        events = [call.args[0] for call in log.await_args_list]
        self.assertEqual(events.count("DEMO_OPEN_CONFIRMATION"), 1)
        self.assertIn("DEMO_OPEN_CONFIRMATION_RESET", events)
        self.assertNotIn("DEMO_BET_ACCEPTANCE_CONFIRMED", events)

    async def test_selected_team_goal_during_final_guard_is_not_accepted(self):
        baseline = snapshot(3, 0)
        selected_team_goal = snapshot(3, 1)
        self.engine._read_fresh_score = AsyncMock(
            side_effect=[
                baseline,
                baseline,
                baseline,
                selected_team_goal,
            ]
        )
        self.engine._demo_prebet_canvas_is_blocked = AsyncMock(return_value=False)

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            with self.assertRaises(MissedSelectedTeamGoal):
                await self.engine._confirm_demo_bet_acceptance(
                    page=object(),
                    browser=object(),
                    selected_match=self.selected_match,
                    current_odds=self.odds,
                    window=self.window,
                    snapshot=baseline,
                )

        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("DEMO_SCORE_CHANGED_BEFORE_ACCEPTANCE", events)
        self.assertIn("DEMO_MISSED_SELECTED_TEAM_GOAL", events)
        self.assertNotIn("DEMO_BET_ACCEPTANCE_CONFIRMED", events)


if __name__ == "__main__":
    unittest.main()
