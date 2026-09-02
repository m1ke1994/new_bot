from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .strategy import money


DEMO_START_BUDGET = Decimal("4142.00")


@dataclass
class DemoBudget:
    initial_budget: Decimal = DEMO_START_BUDGET
    current_budget: Decimal = DEMO_START_BUDGET
    _settled_bet_ids: set[str] = field(default_factory=set, repr=False)

    @property
    def session_profit(self) -> Decimal:
        return money(self.current_budget - self.initial_budget)

    def restore(self, initial_budget: Decimal | int | float | str, current_budget: Decimal | int | float | str) -> None:
        self.initial_budget, self.current_budget = money(initial_budget), money(current_budget)

    def snapshot(self) -> dict[str, float]:
        return {"initial_budget": float(self.initial_budget), "current_budget": float(self.current_budget), "session_profit": float(self.session_profit)}

    def settle(self, bet_id: str, result: str, stake: Decimal | int | float | str, actual_odds: Decimal | int | float | str = 2) -> dict[str, Any] | None:
        """Apply one bet exactly once. Win P&L uses the odds stored on that bet."""
        if bet_id in self._settled_bet_ids:
            return None
        stake_value, odds = money(stake), Decimal(str(actual_odds))
        if result not in {"WIN", "LOSE"} or stake_value <= 0 or odds <= 1:
            raise ValueError("Invalid DEMO settlement")
        before = self.current_budget
        gross_return = money(stake_value * odds) if result == "WIN" else Decimal("0.00")
        change = money(gross_return - stake_value) if result == "WIN" else -stake_value
        self.current_budget = money(self.current_budget + change)
        self._settled_bet_ids.add(bet_id)
        return {"budget_before": float(before), "budget_change": float(change), "budget_after": float(self.current_budget), "pnl": float(change), "gross_return": float(gross_return)}
