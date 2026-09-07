import unittest
from decimal import Decimal

from backend.app.demo.models import NextGoalOdds, Score, Scorer
from backend.app.match_filters import (
    EXCLUDED_TEAMS,
    MIN_INITIAL_SELECTED_ODDS,
    is_excluded_match,
    is_initial_odds_allowed,
)
from backend.app.demo.strategy import (
    BET_STEPS,
    ScoreProgression,
    StrategyConfig,
    detect_scorer,
    odds_for_selected_side,
    select_team_with_higher_odds,
    validate_score_progression,
)


class StrategyTests(unittest.TestCase):
    def test_match_filter_flags_default_to_enabled(self):
        config = StrategyConfig.from_payload({})
        self.assertTrue(config.exclude_teams_enabled)
        self.assertTrue(config.min_initial_odds_enabled)
        self.assertTrue(config.to_dict()["exclude_teams_enabled"])
        self.assertTrue(config.to_dict()["min_initial_odds_enabled"])

    def test_disabled_match_filters_allow_previously_rejected_values(self):
        self.assertTrue(is_excluded_match("Chelsea", "Lille", enabled=True))
        self.assertFalse(is_excluded_match("Chelsea", "Lille", enabled=False))
        self.assertTrue(is_initial_odds_allowed(1.92, enabled=False))

    def test_filter_switches_are_independent(self):
        self.assertFalse(is_excluded_match("Chelsea", "Lille", enabled=False))
        self.assertFalse(is_initial_odds_allowed(1.80, enabled=True))
        self.assertTrue(is_initial_odds_allowed(1.80, enabled=False))
        self.assertTrue(is_initial_odds_allowed(1.95, enabled=True))

    def test_both_disabled_apply_no_additional_match_filters(self):
        config = StrategyConfig.from_payload(
            {
                "exclude_teams_enabled": False,
                "min_initial_odds_enabled": False,
            }
        )
        self.assertFalse(
            is_excluded_match(
                "Chelsea",
                "Roma",
                enabled=config.exclude_teams_enabled,
            )
        )
        self.assertTrue(
            is_initial_odds_allowed(
                1.80,
                enabled=config.min_initial_odds_enabled,
            )
        )

    def test_strategy_config_rejects_non_boolean_filter_values(self):
        with self.assertRaises(ValueError):
            StrategyConfig.from_payload({"exclude_teams_enabled": "false"})

    def test_initial_odds_threshold_is_inclusive_and_decimal_safe(self):
        self.assertEqual(MIN_INITIAL_SELECTED_ODDS, Decimal("1.93"))
        self.assertFalse(is_initial_odds_allowed(1.92))
        self.assertFalse(is_initial_odds_allowed("1.929"))
        self.assertTrue(is_initial_odds_allowed(1.93))
        self.assertTrue(is_initial_odds_allowed(2.05))

    def test_excluded_teams_are_exact_casefolded_names(self):
        self.assertEqual(EXCLUDED_TEAMS, {"chelsea", "челси", "roma", "рома"})
        for team in (
            "Chelsea",
            " CHELSEA ",
            "Челси",
            "чЕЛСИ",
            "Roma",
            " Рома ",
        ):
            with self.subTest(team=team):
                self.assertTrue(is_excluded_match(team, "Lille"))
                self.assertTrue(is_excluded_match("Lille", team))

    def test_team_filter_does_not_use_broad_substrings(self):
        self.assertFalse(is_excluded_match("Chelsea U21", "Lille"))
        self.assertFalse(is_excluded_match("Romarinho", "Nice"))
        self.assertFalse(is_excluded_match("Arsenal", "Milan"))

    def test_default_stake_row_is_valid_but_not_used_as_runtime_config(self):
        self.assertEqual(len(BET_STEPS), 7)
        self.assertTrue(all(amount > 0 for amount in BET_STEPS))

    def test_manual_stakes_are_the_final_source_of_truth(self):
        config = StrategyConfig.from_payload(
            {
                "initial_stake": 57,
                "progression_multiplier": 2.25,
                "max_steps": 7,
                "stakes": [57, 128, 288, 648, 1458, 3281, 7589],
            }
        )
        self.assertEqual(config.to_dict()["stakes"], [57.0, 128.0, 288.0, 648.0, 1458.0, 3281.0, 7589.0])
        self.assertEqual(config.to_dict()["required_budget"], 13449.0)

    def test_score_progression_detects_a_missed_goal(self):
        self.assertEqual(validate_score_progression(Score(0, 1), Score(1, 1), expected_goals=0), ScoreProgression.MISSED_EVENT)
        self.assertEqual(validate_score_progression(Score(0, 0), Score(0, 1), expected_goals=1), ScoreProgression.EXPECTED_GOAL)

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

    def test_selected_side_stays_fixed_when_later_odds_reverse(self):
        selection = select_team_with_higher_odds(
            "Лилль",
            "Базель",
            NextGoalOdds(team1=1.784, team2=1.984),
        )
        selected, opponent = odds_for_selected_side(
            NextGoalOdds(team1=2.10, team2=1.80),
            selection.selected_side,
        )
        self.assertEqual(selection.selected_team, "Базель")
        self.assertEqual(selection.selected_side, Scorer.TEAM_2)
        self.assertEqual((selected, opponent), (1.80, 2.10))

    def test_reference_lose_lose_win_series_keeps_team_and_steps(self):
        selection = select_team_with_higher_odds(
            "Лилль",
            "Базель",
            NextGoalOdds(team1=1.784, team2=1.984),
        )
        scores = [Score(0, 0), Score(1, 0), Score(2, 0), Score(2, 1)]
        results = []

        for previous, current in zip(scores[:-1], scores[1:], strict=True):
            scorer = detect_scorer(previous, current)
            results.append("WIN" if scorer == selection.selected_side else "LOSE")

        self.assertEqual(results, ["LOSE", "LOSE", "WIN"])
        self.assertEqual(len(BET_STEPS), 7)
        self.assertEqual(selection.selected_team, "Базель")
        self.assertEqual(selection.selected_side, Scorer.TEAM_2)


if __name__ == "__main__":
    unittest.main()
