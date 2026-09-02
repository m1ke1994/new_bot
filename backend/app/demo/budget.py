from dataclasses import dataclass, field
from typing import Any


DEMO_START_BUDGET = 4142


@dataclass
class DemoBudget:
    initial_budget: int = DEMO_START_BUDGET
    current_budget: int = DEMO_START_BUDGET
    _settled_bet_ids: set[str] = field(default_factory=set, repr=False)

    @property
    def session_profit(self) -> int:
        return self.current_budget - self.initial_budget

    def reset(self) -> None:
        self.current_budget = self.initial_budget
        self._settled_bet_ids.clear()

    def snapshot(self) -> dict[str, int]:
        return {
            "initial_budget": self.initial_budget,
            "current_budget": self.current_budget,
            "session_profit": self.session_profit,
        }

    def settle(self, bet_id: str, result: str, stake: int) -> dict[str, Any] | None:
        """Apply one DEMO result exactly once and return its budget journal fields."""
        if bet_id in self._settled_bet_ids:
            return None
        if result not in {"WIN", "LOSE"}:
            raise ValueError(f"Неподдерживаемый результат DEMO-ставки: {result}")
        if stake <= 0:
            raise ValueError("Сумма DEMO-ставки должна быть положительной.")

        before = self.current_budget
        change = stake if result == "WIN" else -stake
        self.current_budget += change
        self._settled_bet_ids.add(bet_id)
        return {
            "budget_before": before,
            "budget_change": change,
            "budget_after": self.current_budget,
        }
