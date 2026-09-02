import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backend.app.demo.budget import DEMO_START_BUDGET, DemoBudget
from backend.app.demo.history import DemoRepository


class DemoBudgetTests(unittest.TestCase):
    def test_win_uses_actual_odds_and_keeps_cents(self):
        budget = DemoBudget()
        settled = budget.settle("odds-1", "WIN", 128, 1.80)

        self.assertEqual(settled["gross_return"], 230.40)
        self.assertEqual(settled["pnl"], 102.40)
        self.assertEqual(budget.current_budget, DEMO_START_BUDGET + Decimal("102.40"))

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
    async def test_configuration_budget_and_sequence_survive_new_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = DemoRepository(path)
            await first.save_config({"initial_stake": 57, "progression_multiplier": 2.25, "max_steps": 3, "stakes": [57, 128, 288]})
            await first.save_budget({"initial_budget": 4142, "current_budget": 4014.40, "session_profit": -127.60})
            await first.save_sequence(current_step=3, status="WAITING_NEXT_MATCH", cumulative_pnl="-127.60")

            restored = DemoRepository(path)
            self.assertEqual((await restored.get_config())["stakes"], [57.0, 128.0, 288.0])
            self.assertEqual((await restored.get_budget())["current_budget"], 4014.40)
            self.assertEqual((await restored.get_sequence())["current_step"], 3)
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
