import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.app.browser.market import MarketNotAvailable
from backend.app.demo.engine import DemoEngine, ModeConflictError
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import (
    NextGoalOdds,
    Score,
    ScoreboardSnapshot,
    Scorer,
    TeamSelection,
)
from backend.app.live.executor import BLOCKED_EVENT_SIGNAL
from backend.app.live.models import PendingLiveBet, PlacementObservation


def snapshot(score1, score2):
    return ScoreboardSnapshot("TEAM 1", "TEAM 2", Score(score1, score2), "01:00", "1-й тайм")


class FakeManager:
    generation = 1

    def set_logger(self, _logger):
        pass

    async def ensure_page(self):
        return object()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class LivePendingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_blocked_recovery(
        self,
        score_before_removal: ScoreboardSnapshot,
        score_after_removal: ScoreboardSnapshot,
    ):
        initial = snapshot(1, 1)
        initial_odds = NextGoalOdds(
            1.80,
            2.03,
            next_goal_number=3,
            team1_locator=object(),
            team2_locator=object(),
        )
        retry_goal = (
            score_after_removal.score.team1
            + score_after_removal.score.team2
            + 1
        )
        retry_odds = NextGoalOdds(
            1.82,
            2.04,
            next_goal_number=retry_goal,
            team1_locator=object(),
            team2_locator=object(),
        )
        selection = TeamSelection(
            "TEAM 2",
            Scorer.TEAM_2,
            2.03,
            "TEAM 1",
            1.80,
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                current_match_id="match",
                selected_team="TEAM 2",
            )
            budget_before = (await repository.get_budget())["current_budget"]
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._pending_live_bet = PendingLiveBet(
                    "match_TEAM_2_step3",
                    "match",
                    "TEAM 2",
                    Scorer.TEAM_2,
                    3,
                    288,
                    3,
                )
                engine.live_executor.prepare = AsyncMock()
                engine.live_executor.manual_click_seen = AsyncMock(return_value=True)
                engine.live_executor.invalidate = AsyncMock()
                engine.live_executor.remove_blocked_coupon = AsyncMock(
                    return_value=True
                )
                engine.live_executor.market_was_selected = Mock(return_value=True)
                engine._publish_pending_bet = AsyncMock()
                engine._wait_for_odds = AsyncMock(
                    return_value=(score_after_removal, retry_odds)
                )
                engine._wait_for_live_confirmation_or_score = AsyncMock(
                    side_effect=[
                        (
                            PlacementObservation(
                                False,
                                BLOCKED_EVENT_SIGNAL,
                                retryable=True,
                            ),
                            initial,
                        ),
                        (PlacementObservation(True, "accepted"), score_after_removal),
                    ]
                )
                engine._read_fresh_score = AsyncMock(
                    side_effect=[
                        initial,
                        score_before_removal,
                        score_after_removal,
                        score_after_removal,
                        score_after_removal,
                        score_after_removal,
                    ]
                )

                result = await engine._prepare_live_until_placed(
                    browser=object(),
                    selected_match={"match_id": "match"},
                    selection=selection,
                    match_name="TEAM 1 — TEAM 2",
                    cycle_id="cycle",
                    step=3,
                    amount=288,
                    snapshot=initial,
                    current_odds=initial_odds,
                    record={"mode": "LIVE"},
                )

                history = await repository.history(mode="LIVE")
                sequence = await repository.get_sequence()
                budget_after = (await repository.get_budget())["current_budget"]
                logs = await repository.logs()

        decisions = [
            call.args[1]
            for call in engine.live_executor.prepare.await_args_list
        ]
        self.assertIsNotNone(result)
        self.assertEqual(len(decisions), 2)
        self.assertEqual([item.strategy_step for item in decisions], [3, 3])
        self.assertEqual([item.amount for item in decisions], [288, 288])
        self.assertEqual([item.team for item in decisions], ["TEAM 2", "TEAM 2"])
        self.assertEqual([item.goal_number for item in decisions], [3, retry_goal])
        self.assertEqual(sequence["current_step"], 3)
        self.assertEqual(sequence["selected_team"], "TEAM 2")
        self.assertEqual(budget_after, budget_before)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["result"], "ACTIVE")
        self.assertEqual(history[0]["step"], 3)
        self.assertEqual(history[0]["next_goal_number"], retry_goal)
        self.assertNotIn("WIN", [item["event"] for item in logs])
        self.assertNotIn("LOSE", [item["event"] for item in logs])
        engine.live_executor.remove_blocked_coupon.assert_awaited_once()
        return engine, logs

    async def test_blocked_pending_retries_same_step_without_score_change(self):
        await self._run_blocked_recovery(snapshot(1, 1), snapshot(1, 1))

    async def test_blocked_pending_retries_same_step_after_one_goal(self):
        await self._run_blocked_recovery(snapshot(2, 1), snapshot(2, 1))

    async def test_blocked_pending_uses_latest_score_after_two_changes(self):
        engine, logs = await self._run_blocked_recovery(
            snapshot(2, 1),
            snapshot(3, 1),
        )

        self.assertEqual(engine._active_live_bet.goal_number, 5)
        self.assertIn(
            "LIVE_SCORE_CHANGED_DURING_BLOCKED_RECOVERY",
            [item["event"] for item in logs],
        )

    def test_score_updates_only_goal_number_of_pending_decision(self):
        pending = PendingLiveBet(
            "match_TEAM_2_step2",
            "match",
            "TEAM 2",
            Scorer.TEAM_2,
            2,
            42,
            2,
        )

        pending = pending.with_score(Score(2, 0)).with_score(Score(2, 1))

        self.assertEqual(pending.strategy_step, 2)
        self.assertEqual(pending.amount, 42)
        self.assertEqual(pending.team, "TEAM 2")
        self.assertEqual(pending.target_goal_number, 4)

    async def test_market_locked_keeps_step_and_uses_current_goal_when_available(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._pending_live_bet = PendingLiveBet(
            "match_TEAM_2_step2",
            "match",
            "TEAM 2",
            Scorer.TEAM_2,
            2,
            42,
            2,
        )
        engine._sleep_or_stop = AsyncMock()
        snapshots = iter([snapshot(1, 0), snapshot(2, 0), snapshot(2, 1), snapshot(2, 1)])

        class FakeMatchBrowser:
            def __init__(self, _page):
                pass

            async def snapshot(self):
                return next(snapshots)

        async def read_odds(_page, _team1, _team2, score1, score2, _logger):
            goal = score1 + score2 + 1
            if goal < 4:
                raise MarketNotAvailable("locked", status="MARKET_LOCKED")
            return NextGoalOdds(1.8, 2.03, next_goal_number=goal)

        with (
            patch("backend.app.demo.engine.MatchBrowser", FakeMatchBrowser),
            patch("backend.app.demo.engine.read_next_goal_odds", new=read_odds),
            patch("backend.app.demo.engine.REPOSITORY.log", new=AsyncMock()),
            patch("backend.app.demo.engine.REPOSITORY.save_bet", new=AsyncMock()) as save_bet,
        ):
            current, odds = await engine._wait_for_odds(snapshot(1, 0), {"match_id": "match"})

        self.assertEqual(current.score, Score(2, 1))
        self.assertEqual(odds.next_goal_number, 4)
        self.assertEqual(engine._pending_live_bet.strategy_step, 2)
        self.assertEqual(engine._pending_live_bet.amount, 42)
        self.assertEqual(engine._pending_live_bet.team, "TEAM 2")
        self.assertEqual(engine._pending_live_bet.target_goal_number, 4)
        save_bet.assert_not_awaited()

    async def test_unresolved_manual_submission_blocks_live_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {
                    "id": "unknown",
                    "mode": "LIVE",
                    "result": "SUBMISSION_UNKNOWN",
                    "status": "AWAITING_PLACEMENT_RESULT",
                }
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())

                with self.assertRaises(ModeConflictError):
                    await engine.start("LIVE")

    async def test_crash_after_dom_active_signal_blocks_duplicate_live_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {
                    "id": "pending-after-active-signal",
                    "mode": "LIVE",
                    "result": "PENDING",
                    "status": "ACTIVE",
                }
            )
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(FakeManager())

                with self.assertRaises(ModeConflictError):
                    await engine.start("LIVE")


if __name__ == "__main__":
    unittest.main()
