import unittest

from backend.app.table_tennis.fork_math import (
    calculate_arbitrage_percent,
    calculate_fork,
    calculate_hedge_stake,
    calculate_outcome_profit,
    calculate_series_settlement,
    calculate_zero_hedge_odd,
    is_arbitrage_available,
    is_zero_zero_score,
    minimum_second_odds,
    select_favorite,
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
        self.assertAlmostEqual(result.fork_percent, 1.4605, places=4)

    def test_second_odd_below_zero_fork_threshold_is_rejected(self):
        result = calculate_fork(1000, 2.07, 1.90)

        self.assertFalse(result.profitable)
        self.assertLess(result.guaranteed_profit, 0)

    def test_150_has_zero_boundary_at_300_and_requires_positive_margin(self):
        self.assertEqual(calculate_zero_hedge_odd(1.50), 3.0)
        self.assertEqual(calculate_arbitrage_percent(1.50, 3.00), 0.0)
        self.assertFalse(is_arbitrage_available(1.50, 3.00, min_arb_percent=0.0))
        self.assertGreater(calculate_arbitrage_percent(1.50, 3.01), 0.0)
        self.assertTrue(is_arbitrage_available(1.50, 3.01, min_arb_percent=0.0))
        self.assertFalse(is_arbitrage_available(1.50, 3.01))

    def test_hedge_stake_and_both_outcome_profits_match_reference(self):
        second_stake = calculate_hedge_stake(100, 1.50, 3.10)
        profit_first, profit_second = calculate_outcome_profit(
            100,
            1.50,
            second_stake,
            3.10,
        )

        self.assertEqual(second_stake, 48.39)
        self.assertEqual(profit_first, 1.61)
        self.assertEqual(profit_second, 1.62)

    def test_first_leg_always_selects_the_lower_odd(self):
        self.assertEqual(select_favorite(1.34, 4.11), "p1")
        self.assertEqual(select_favorite(3.75, 1.29), "p2")

    def test_equal_odds_within_epsilon_have_no_favorite(self):
        with self.assertRaises(ValueError):
            select_favorite(1.87, 1.87)
        with self.assertRaises(ValueError):
            select_favorite(1.870, 1.874)
        self.assertEqual(select_favorite(1.50, 2.50), "p1")
        self.assertEqual(select_favorite(3.00, 1.40), "p2")

    def test_299_is_not_a_fork_and_310_is_profitable(self):
        self.assertFalse(is_arbitrage_available(1.50, 2.99))
        self.assertTrue(is_arbitrage_available(1.50, 3.10))

    def test_settlement_when_first_leg_wins(self):
        result = calculate_series_settlement(
            10_000, "p1", 1000, 1.50, "p1",
            hedge_side="p2", hedge_stake=483.87, hedge_odds=3.10,
        )
        self.assertEqual(result.balance_after, 10016.13)
        self.assertEqual(result.profit_loss, 16.13)
        self.assertEqual(result.winning_leg, "FIRST_LEG")

    def test_settlement_when_hedge_leg_wins(self):
        result = calculate_series_settlement(
            10_000, "p1", 1000, 1.50, "p2",
            hedge_side="p2", hedge_stake=483.87, hedge_odds=3.10,
        )
        self.assertEqual(result.balance_after, 10016.13)
        self.assertEqual(result.profit_loss, 16.13)
        self.assertEqual(result.winning_leg, "HEDGE_LEG")

    def test_first_leg_only_settlement_records_real_win_and_loss(self):
        won = calculate_series_settlement(10_000, "p1", 1000, 1.50, "p1")
        lost = calculate_series_settlement(10_000, "p1", 1000, 1.50, "p2")
        self.assertEqual((won.balance_after, won.profit_loss), (10500.0, 500.0))
        self.assertEqual((lost.balance_after, lost.profit_loss), (9000.0, -1000.0))


if __name__ == "__main__":
    unittest.main()
