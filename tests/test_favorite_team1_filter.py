import unittest

from backend.app.demo.models import NextGoalOdds, Scorer
from backend.app.demo.strategy import StrategyConfig
from backend.app.favorite_shadow import (
    favorite_team1_entry_selection,
    favorite_team1_rejection_reason,
)


class FavoriteTeam1FilterTests(unittest.TestCase):
    def test_left_side_favorite_is_allowed(self):
        selection = favorite_team1_entry_selection(
            "Alpha",
            "Beta",
            NextGoalOdds(1.78, 2.12),
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.selected_team, "Alpha")
        self.assertEqual(selection.selected_side, Scorer.TEAM_1)
        self.assertEqual(selection.selected_odds, 1.78)
        self.assertEqual(
            favorite_team1_rejection_reason(
                "Alpha", "Beta", NextGoalOdds(1.78, 2.12)
            ),
            None,
        )

    def test_minimum_odds_is_inclusive(self):
        allowed = favorite_team1_entry_selection(
            "Alpha",
            "Beta",
            NextGoalOdds(2.00, 2.30),
            min_odds=2.00,
        )
        rejected = favorite_team1_entry_selection(
            "Alpha",
            "Beta",
            NextGoalOdds(1.99, 2.30),
            min_odds=2.00,
        )
        self.assertIsNotNone(allowed)
        self.assertIsNone(rejected)
        self.assertEqual(
            favorite_team1_rejection_reason(
                "Alpha",
                "Beta",
                NextGoalOdds(1.99, 2.30),
                min_odds=2.00,
            ),
            "FAVORITE_ODDS_BELOW_MIN",
        )

    def test_right_side_favorite_is_rejected(self):
        selection = favorite_team1_entry_selection(
            "Alpha",
            "Beta",
            NextGoalOdds(2.14, 1.76),
        )
        self.assertIsNone(selection)
        self.assertEqual(
            favorite_team1_rejection_reason(
                "Alpha", "Beta", NextGoalOdds(2.14, 1.76)
            ),
            "FAVORITE_IS_TEAM_2",
        )

    def test_equal_odds_are_rejected(self):
        selection = favorite_team1_entry_selection(
            "Alpha",
            "Beta",
            NextGoalOdds(1.95, 1.95),
        )
        self.assertIsNone(selection)
        self.assertEqual(
            favorite_team1_rejection_reason(
                "Alpha", "Beta", NextGoalOdds(1.95, 1.95)
            ),
            "EQUAL_INITIAL_ODDS",
        )

    def test_filter_is_independent_and_disabled_by_default(self):
        disabled = StrategyConfig.from_payload({})
        enabled = StrategyConfig.from_payload({"favorite_team1_enabled": True})
        self.assertFalse(disabled.favorite_team1_enabled)
        self.assertTrue(enabled.favorite_team1_enabled)
        self.assertTrue(enabled.to_dict()["favorite_team1_enabled"])
        self.assertEqual(enabled.favorite_team1_min_odds, 2)
        configured = StrategyConfig.from_payload(
            {"favorite_team1_enabled": True, "favorite_team1_min_odds": 2.05}
        )
        self.assertEqual(configured.favorite_team1_min_odds, 2.05)
        self.assertEqual(configured.to_dict()["favorite_team1_min_odds"], 2.05)

    def test_conflicting_team1_filters_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            StrategyConfig.from_payload(
                {
                    "favorite_team1_enabled": True,
                    "team1_profile_enabled": True,
                }
            )

    def test_engine_tracks_rejected_favorite_matches_for_current_run(self):
        from pathlib import Path

        source = Path("backend/app/demo/engine.py").read_text(encoding="utf-8")
        self.assertIn("_favorite_team1_rejected_match_ids", source)
        self.assertIn("FAVORITE_TEAM1_REJECTED_MATCH_SKIPPED", source)
        self.assertIn("FAVORITE_TEAM1_SWITCHING_TO_NEXT_MATCH", source)
        self.assertIn("_favorite_team1_rejected_match_ids.clear()", source)


if __name__ == "__main__":
    unittest.main()
