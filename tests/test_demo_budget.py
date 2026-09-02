import tempfile
import unittest
from pathlib import Path

from backend.app.demo.budget import DEMO_START_BUDGET, DemoBudget
from backend.app.demo.history import DemoRepository


class DemoBudgetTests(unittest.TestCase):
    def test_reference_budget_sequence(self):
        budget = DemoBudget()

        first = budget.settle("bet-1", "LOSE", 20)
        second = budget.settle("bet-2", "LOSE", 44)
        third = budget.settle("bet-3", "WIN", 97)

        self.assertEqual(first["budget_after"], 4122)
        self.assertEqual(second["budget_after"], 4078)
        self.assertEqual(third["budget_after"], 4175)
        self.assertEqual(budget.current_budget, 4175)
        self.assertEqual(budget.session_profit, 33)
        self.assertEqual(budget.initial_budget, DEMO_START_BUDGET)

    def test_same_bet_cannot_change_budget_twice(self):
        budget = DemoBudget()
        applied = budget.settle("same-bet", "WIN", 97)
        duplicate = budget.settle("same-bet", "WIN", 97)

        self.assertIsNotNone(applied)
        self.assertIsNone(duplicate)
        self.assertEqual(budget.current_budget, 4239)


class DemoJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_bet_is_updated_in_place_when_settled(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {
                    "id": "bet-1",
                    "amount": 20,
                    "odds": 1.984,
                    "step": 1,
                    "result": "ACTIVE",
                    "status": "ACTIVE",
                    "budget_before": 4142,
                }
            )
            await repository.save_bet(
                {
                    "id": "bet-1",
                    "result": "LOSE",
                    "status": "SETTLED",
                    "settled": True,
                    "budget_change": -20,
                    "budget_after": 4122,
                }
            )

            history = await repository.history()
            stats = await repository.stats()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["result"], "LOSE")
            self.assertEqual(history[0]["budget_after"], 4122)
            self.assertEqual(stats["bets"], 1)
            self.assertEqual(stats["losses"], 1)


if __name__ == "__main__":
    unittest.main()
