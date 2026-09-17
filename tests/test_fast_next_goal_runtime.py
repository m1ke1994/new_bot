import unittest
from types import SimpleNamespace

from backend.app.demo.fast_next_goal_runtime import (
    FAST_PREBET_CACHE_USES,
    _cache_fast_snapshot,
    _consume_fast_snapshot,
    _selected_and_opponent_odds,
    _selected_side_is_locked,
    _side_number,
)
from backend.app.demo.engine import selected_team_scored_between
from backend.app.demo.models import NextGoalOdds, Score, Scorer


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

    def test_only_selected_side_lock_blocks_virtual_bet(self):
        odds = NextGoalOdds(
            team1=2.11,
            team2=1.77,
            locked_sides=(2,),
        )
        self.assertFalse(_selected_side_is_locked(Scorer.TEAM_1, odds))
        self.assertTrue(_selected_side_is_locked(Scorer.TEAM_2, odds))

    def test_selected_and_opponent_odds_follow_fixed_side(self):
        odds = NextGoalOdds(team1=1.82, team2=2.07)
        self.assertEqual(_side_number(Scorer.TEAM_2), 2)
        self.assertEqual(
            _selected_and_opponent_odds(Scorer.TEAM_2, odds),
            (2.07, 1.82),
        )

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
