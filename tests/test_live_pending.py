import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.browser.market import MarketNotAvailable
from backend.app.demo.engine import DemoEngine, ModeConflictError
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, ScoreboardSnapshot, Scorer
from backend.app.live.models import PendingLiveBet


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
