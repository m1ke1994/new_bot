import unittest

from backend.app.demo.strategy import StrategyConfig, StrategyType


class OneBetPerMatchConfigTests(unittest.TestCase):
    def test_enabled_mode_round_trip(self):
        config = StrategyConfig.from_payload({
            "strategy_type": "NEXT_GOAL",
            "one_bet_per_match_enabled": True,
            "favorite_team1_enabled": False,
        })
        self.assertTrue(config.one_bet_per_match_enabled)
        self.assertEqual(config.strategy_type, StrategyType.NEXT_GOAL)
        self.assertTrue(StrategyConfig.from_payload(config.to_dict()).one_bet_per_match_enabled)

    def test_default_off_keeps_existing_behavior(self):
        self.assertFalse(StrategyConfig.from_payload({}).one_bet_per_match_enabled)

    def test_favorite_selection_conflict(self):
        with self.assertRaises(ValueError):
            StrategyConfig.from_payload({
                "one_bet_per_match_enabled": True,
                "favorite_team1_enabled": True,
            })

    def test_long_series_conflict(self):
        with self.assertRaises(ValueError):
            StrategyConfig.from_payload({
                "one_bet_per_match_enabled": True,
                "long_series_enabled": True,
            })


if __name__ == "__main__":
    unittest.main()
