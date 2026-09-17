import unittest

from backend.app.demo.budget import DEMO_START_BUDGET
from backend.app.demo.budget_sync import (
    rebase_budget_snapshot,
    required_budget_from_config,
)


class DynamicBudgetTests(unittest.TestCase):
    def test_default_budget_matches_default_stake_row(self):
        self.assertEqual(float(DEMO_START_BUDGET), 4099.0)

    def test_required_budget_is_sum_of_manual_stakes(self):
        config = {
            "initial_stake": 20,
            "progression_multiplier": 2.2,
            "max_steps": 7,
            "stakes": [20, 44, 96, 211, 464, 1020, 2244],
        }
        self.assertEqual(float(required_budget_from_config(config)), 4099.0)

    def test_rebase_preserves_session_profit(self):
        config = {
            "initial_stake": 20,
            "progression_multiplier": 2.2,
            "max_steps": 7,
            "stakes": [20, 44, 96, 211, 464, 1020, 2244],
        }
        legacy_budget = {
            "initial_budget": 4142.0,
            "current_budget": 4212.76,
            "session_profit": 70.76,
        }

        rebased = rebase_budget_snapshot(config, legacy_budget)

        self.assertEqual(rebased["initial_budget"], 4099.0)
        self.assertEqual(rebased["current_budget"], 4169.76)
        self.assertEqual(rebased["session_profit"], 70.76)


if __name__ == "__main__":
    unittest.main()
