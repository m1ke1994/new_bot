import unittest
from types import SimpleNamespace

from backend.app.demo.fast_next_goal_runtime import (
    FAST_PREBET_CACHE_USES,
    _cache_fast_snapshot,
    _consume_fast_snapshot,
)
from backend.app.demo.engine import selected_team_scored_between
from backend.app.demo.models import Score, Scorer


class FastNextGoalRuntimeTests(unittest.TestCase):
    @staticmethod
    def snapshot(score: Score):
        return SimpleNamespace(team1="A", team2="B", score=score, period="1-й тайм")

    def test_odds_frame_can_skip_both_immediate_prebet_rechecks(self):
        engine = SimpleNamespace()
        captured = self.snapshot(Score(1, 0))
        _cache_fast_snapshot(engine, captured)

        self.assertEqual(FAST_PREBET_CACHE_USES, 2)
        self.assertIs(_consume_fast_snapshot(engine, captured), captured)
        self.assertIs(_consume_fast_snapshot(engine, captured), captured)
        self.assertIsNone(_consume_fast_snapshot(engine, captured))

    def test_score_change_invalidates_cached_prebet_snapshot(self):
        engine = SimpleNamespace()
        captured = self.snapshot(Score(1, 0))
        changed = self.snapshot(Score(2, 0))
        _cache_fast_snapshot(engine, captured)

        self.assertIsNone(_consume_fast_snapshot(engine, changed))
        self.assertEqual(engine._fast_demo_odds_snapshot_uses, 0)

    def test_opponent_goal_during_block_keeps_selected_team_unscored(self):
        before = Score(1, 0)
        opponent_scored = Score(2, 0)
        selected_scored = Score(1, 1)

        self.assertFalse(
            selected_team_scored_between(before, opponent_scored, Scorer.TEAM_2)
        )
        self.assertTrue(
            selected_team_scored_between(before, selected_scored, Scorer.TEAM_2)
        )


if __name__ == "__main__":
    unittest.main()
