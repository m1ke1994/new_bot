import unittest

from backend.app.table_tennis.fork_math import (
    calculate_fork,
    is_zero_zero_score,
    minimum_second_odds,
    select_zero_zero_matches,
    zero_fork_capital_required,
)
from backend.app.table_tennis.models import TableTennisMatch


class TableTennisForkMathTests(unittest.TestCase):
    def make_match(self, event_id: str, score: str | None, time: str | None = None):
        return TableTennisMatch(
            event_id=event_id,
            league_id="1",
            league_name="League",
            player_1=f"P1-{event_id}",
            player_2=f"P2-{event_id}",
            score=score,
            sets_score=None,
            current_set=1,
            points_player_1=None,
            points_player_2=None,
            status="LIVE",
            started=True,
            time=time,
            href=f"/event/{event_id}",
            url=f"https://example.test/event/{event_id}",
        )

    def test_zero_zero_score_accepts_common_delimiters(self):
        self.assertTrue(is_zero_zero_score("0:0"))
        self.assertTrue(is_zero_zero_score("0 - 0"))
        self.assertTrue(is_zero_zero_score("0–0"))
        self.assertFalse(is_zero_zero_score("1:0"))
        self.assertFalse(is_zero_zero_score(None))

    def test_zero_zero_matches_only(self):
        first = self.make_match("1", "0:0")
        second = self.make_match("2", "1:0")
        third = self.make_match("3", "0 - 0")

        selected = select_zero_zero_matches([first, second, third])

        self.assertEqual({item.event_id for item in selected}, {"1", "3"})

    def test_minimum_second_odds_for_207(self):
        self.assertAlmostEqual(minimum_second_odds(2.07), 1.9345794392, places=8)

    def test_zero_fork_budget_for_1000_at_207(self):
        self.assertEqual(zero_fork_capital_required(1000, 2.07), 2070.0)

    def test_example_207_and_199_is_profitable(self):
        result = calculate_fork(1000, 2.07, 1.99)

        self.assertTrue(result.profitable)
        self.assertEqual(result.second_stake, 1040.2)
        self.assertEqual(result.total_stake, 2040.2)
        self.assertEqual(result.guaranteed_profit, 29.8)
        self.assertAlmostEqual(result.fork_percent, 1.4606, places=4)

    def test_second_odd_below_zero_fork_threshold_is_rejected(self):
        result = calculate_fork(1000, 2.07, 1.90)

        self.assertFalse(result.profitable)
        self.assertLess(result.guaranteed_profit, 0)


if __name__ == "__main__":
    unittest.main()
