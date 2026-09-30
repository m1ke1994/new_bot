import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser.market import MarketNotAvailable
from backend.app.demo.engine import (
    DEMO_ACCEPTANCE_CONFIRMATIONS,
    DemoBlockedWindow,
    DemoEngine,
    MissedSelectedTeamGoal,
)
from backend.app.demo.models import (
    NextGoalOdds,
    Score,
    ScoreboardSnapshot,
    Scorer,
    TeamSelection,
)


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

    async def test_virtual_preview_stale_market_stays_in_same_match_and_step(self):
        baseline = snapshot(0, 0)
        retry_odds = NextGoalOdds(
            1.82,
            1.94,
            next_goal_number=1,
            team1_locator=object(),
            team2_locator=object(),
            source="CANVAS_2D",
        )
        selection = TeamSelection(
            "TEAM 2",
            Scorer.TEAM_2,
            1.94,
            "TEAM 1",
            1.82,
        )
        selected_match = {
            "match_id": "match",
            "team1": "TEAM 1",
            "team2": "TEAM 2",
        }
        self.engine.live_executor.preview_virtual_coupon = AsyncMock(
            side_effect=[
                MarketNotAvailable(
                    "Коэффициент изменился перед Canvas-кликом; рынок нужно перечитать."
                ),
                "READY",
            ]
        )
        self.engine.live_executor.clear_unaccepted_coupon = AsyncMock(
            return_value=True
        )
        self.engine._read_fresh_score = AsyncMock(
            side_effect=[baseline, baseline]
        )
        self.engine._wait_for_odds = AsyncMock(
            return_value=(baseline, retry_odds)
        )

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            result = await self.engine._prepare_demo_virtual_until_ready(
                page=object(),
                browser=object(),
                selected_match=selected_match,
                selection=selection,
                cycle_id="cycle",
                step=2,
                amount=23,
                snapshot=baseline,
                current_odds=retry_odds,
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            self.engine.live_executor.preview_virtual_coupon.await_count,
            2,
        )
        decisions = [
            call.args[1]
            for call in self.engine.live_executor.preview_virtual_coupon.await_args_list
        ]
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 2"])
        self.assertEqual([item.strategy_step for item in decisions], [2, 2])
        self.assertEqual([item.amount for item in decisions], [23, 23])
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("DEMO_VIRTUAL_MARKET_STALE", events)
        self.assertIn("DEMO_VIRTUAL_COUPON_VALIDATED", events)

    async def test_virtual_preview_does_not_reclick_until_coupon_is_clear(self):
        baseline = snapshot(0, 0)
        retry_odds = NextGoalOdds(
            1.82,
            1.94,
            next_goal_number=1,
            team1_locator=object(),
            team2_locator=object(),
            source="CANVAS_2D",
        )
        selection = TeamSelection(
            "TEAM 2",
            Scorer.TEAM_2,
            1.94,
            "TEAM 1",
            1.82,
        )
        selected_match = {
            "match_id": "match",
            "team1": "TEAM 1",
            "team2": "TEAM 2",
        }
        self.engine.live_executor.preview_virtual_coupon = AsyncMock(
            side_effect=["BLOCKED", "READY"]
        )
        self.engine.live_executor.clear_unaccepted_coupon = AsyncMock(
            side_effect=[False, False, True, True]
        )
        self.engine._read_fresh_score = AsyncMock(
            side_effect=[baseline, baseline]
        )
        self.engine._wait_for_odds = AsyncMock(
            return_value=(baseline, retry_odds)
        )

        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()) as state_update,
        ):
            result = await self.engine._prepare_demo_virtual_until_ready(
                page=object(),
                browser=object(),
                selected_match=selected_match,
                selection=selection,
                cycle_id="cycle",
                step=2,
                amount=23,
                snapshot=baseline,
                current_odds=retry_odds,
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            self.engine.live_executor.preview_virtual_coupon.await_count,
            2,
        )
        self.assertEqual(
            self.engine.live_executor.clear_unaccepted_coupon.await_count,
            4,
        )
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("DEMO_VIRTUAL_COUPON_CLEAR_WAIT", events)
        self.assertIn("DEMO_VIRTUAL_COUPON_CLEAR_RECOVERED", events)

        phases = [
            call.kwargs["coupon_recovery"]["phase"]
            for call in state_update.await_args_list
            if call.kwargs.get("coupon_recovery")
        ]
        self.assertIn("BLOCKED_COUPON", phases)
        self.assertIn("CLEARING_COUPON", phases)
        self.assertIn("REFRESHING_SCORE", phases)
        self.assertIn("WAITING_NEW_MARKET", phases)
        self.assertIn("RETRYING_SAME_STEP", phases)
        self.assertEqual(phases[-1], "IDLE")

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
