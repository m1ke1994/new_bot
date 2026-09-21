import unittest
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.models import NextGoalOdds, Score, ScoreboardSnapshot, Scorer
from backend.app.demo.strategy import StrategyConfig


class FakeBrowserManager:
    def set_logger(self, logger):
        self.logger = logger


class FakeMatchBrowser:
    def __init__(self, snapshots):
        self.page = object()
        self._snapshots = list(snapshots)

    async def snapshot(self):
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


class ActiveBetLockMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = DemoEngine(FakeBrowserManager())
        self.engine._config = StrategyConfig.from_payload(
            {"blocked_events_switch_enabled": True}
        )
        self.engine._sleep_or_stop = AsyncMock()
        self.engine._status = AsyncMock()
        self.engine._publish_snapshot = AsyncMock()

    @staticmethod
    def snapshot(score1, score2):
        return ScoreboardSnapshot(
            "Team 1",
            "Team 2",
            Score(score1, score2),
            "00:10",
            "1-й тайм",
        )

    async def test_active_bet_logs_lock_and_goal_during_lock(self):
        previous = self.snapshot(0, 7)
        browser = FakeMatchBrowser(
            [
                self.snapshot(0, 7),
                self.snapshot(1, 7),
            ]
        )
        active_odds = NextGoalOdds(
            2.05,
            1.79,
            next_goal_number=8,
            source="CANVAS_2D",
        )
        locked_state = {
            "available": True,
            "locked_sides": (1, 2),
            "recent_locked_sides": (),
            "markers": {
                "1": {
                    "reason": "canvas-path2d-icon",
                    "detected_canvas_id": 3,
                    "seq": 100,
                },
                "2": {
                    "reason": "canvas-path2d-icon",
                    "detected_canvas_id": 3,
                    "seq": 101,
                },
            },
            "recent_markers": {},
            "checked_canvas_ids": (1, 2, 3),
            "released_sides": (),
        }

        with (
            patch(
                "backend.app.demo.engine.read_next_goal_lock_state",
                AsyncMock(side_effect=[locked_state, locked_state]),
            ),
            patch(
                "backend.app.demo.engine.REPOSITORY.log",
                AsyncMock(),
            ) as log,
            patch(
                "backend.app.demo.engine.STATE.update",
                AsyncMock(),
            ),
        ):
            result = await self.engine._wait_for_goal(
                browser,
                {"match_id": "m1", "url": "https://example.test/m1"},
                previous,
                active_odds=active_odds,
                selected_side=Scorer.TEAM_1,
                step=8,
                bet_id="bet-8",
            )

        self.assertIsNotNone(result)
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("ACTIVE_BET_LOCK_MONITOR_STARTED", events)
        self.assertIn("ACTIVE_BET_LOCK_ACTIVE", events)
        self.assertIn("ACTIVE_BET_GOAL_LOCK_CONTEXT", events)
        self.assertIn("ACTIVE_BET_GOAL_DURING_LOCK", events)

    async def test_active_bet_logs_release_without_changing_strategy(self):
        previous = self.snapshot(0, 7)
        browser = FakeMatchBrowser(
            [
                self.snapshot(0, 7),
                self.snapshot(1, 7),
            ]
        )
        active_odds = NextGoalOdds(
            2.05,
            1.79,
            next_goal_number=8,
            source="CANVAS_2D",
        )
        locked_state = {
            "available": True,
            "locked_sides": (1,),
            "recent_locked_sides": (),
            "markers": {
                "1": {
                    "reason": "canvas-path2d-icon",
                    "detected_canvas_id": 3,
                    "seq": 100,
                }
            },
            "recent_markers": {},
            "checked_canvas_ids": (1, 2, 3),
            "released_sides": (),
        }
        released_state = {
            "available": True,
            "locked_sides": (),
            "recent_locked_sides": (),
            "markers": {},
            "recent_markers": {},
            "checked_canvas_ids": (1, 2, 3),
            "released_sides": (1,),
        }

        with (
            patch(
                "backend.app.demo.engine.read_next_goal_lock_state",
                AsyncMock(side_effect=[locked_state, released_state]),
            ),
            patch(
                "backend.app.demo.engine.REPOSITORY.log",
                AsyncMock(),
            ) as log,
            patch(
                "backend.app.demo.engine.STATE.update",
                AsyncMock(),
            ),
        ):
            result = await self.engine._wait_for_goal(
                browser,
                {"match_id": "m1", "url": "https://example.test/m1"},
                previous,
                active_odds=active_odds,
                selected_side=Scorer.TEAM_1,
                step=8,
                bet_id="bet-8",
            )

        self.assertIsNotNone(result)
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("ACTIVE_BET_LOCK_ACTIVE", events)
        self.assertIn("ACTIVE_BET_LOCK_RELEASED", events)
        self.assertIn("ACTIVE_BET_GOAL_LOCK_CONTEXT", events)
        self.assertNotIn("ACTIVE_BET_GOAL_DURING_LOCK", events)


if __name__ == "__main__":
    unittest.main()
