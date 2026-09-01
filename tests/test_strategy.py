import unittest

from backend.app.demo.models import NextGoalOdds, Score, Scorer
from backend.app.demo.strategy import (
    BET_STEPS,
    detect_scorer,
    select_team_with_higher_odds,
)


class StrategyTests(unittest.TestCase):
    def test_bet_steps_are_explicit(self):
        self.assertEqual(
            BET_STEPS,
            [8, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 5904],
        )

    def test_detect_team_1(self):
        self.assertEqual(detect_scorer(Score(1, 2), Score(2, 2)), Scorer.TEAM_1)

    def test_detect_team_2(self):
        self.assertEqual(detect_scorer((1, 2), (1, 3)), Scorer.TEAM_2)

    def test_unchanged_or_invalid_score_is_unknown(self):
        self.assertEqual(detect_scorer((1, 2), (1, 2)), Scorer.UNKNOWN)
        self.assertEqual(detect_scorer((2, 2), (1, 2)), Scorer.UNKNOWN)

    def test_double_change_is_ambiguous(self):
        self.assertEqual(
            detect_scorer((0, 0), (1, 1)),
            Scorer.AMBIGUOUS_SCORE_CHANGE,
        )

    def test_more_than_one_goal_on_one_side_is_ambiguous(self):
        self.assertEqual(
            detect_scorer((0, 0), (2, 0)),
            Scorer.AMBIGUOUS_SCORE_CHANGE,
        )

    def test_selects_higher_odd_and_keeps_side(self):
        result = select_team_with_higher_odds(
            "Ницца",
            "Олимпиакос",
            NextGoalOdds(team1=2.05, team2=1.70),
        )
        self.assertEqual(result.selected_team, "Ницца")
        self.assertEqual(result.selected_side, Scorer.TEAM_1)
        self.assertEqual(result.other_team, "Олимпиакос")

    def test_equal_odds_are_rejected(self):
        with self.assertRaises(ValueError):
            select_team_with_higher_odds(
                "A",
                "B",
                NextGoalOdds(team1=2.0, team2=2.0),
            )


if __name__ == "__main__":
    unittest.main()
