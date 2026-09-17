from decimal import Decimal
from typing import Any

from .history import REPOSITORY
from .state import STATE
from .strategy import StrategyConfig, money


def required_budget_from_config(config_data: dict[str, Any]) -> Decimal:
    config = StrategyConfig.from_payload(config_data)
    return money(sum(config.stakes, Decimal("0")))


def rebase_budget_snapshot(
    config_data: dict[str, Any],
    budget_data: dict[str, Any],
) -> dict[str, float]:
    """Make initial budget equal the stake-row budget while preserving session P&L."""
    initial_budget = required_budget_from_config(config_data)
    session_profit = money(budget_data.get("session_profit", 0))
    current_budget = money(initial_budget + session_profit)
    return {
        "initial_budget": float(initial_budget),
        "current_budget": float(current_budget),
        "session_profit": float(session_profit),
    }


async def sync_strategy_budget(*, update_state: bool = True) -> dict[str, Any]:
    """Persist and expose a budget rebased to the currently saved stake row."""
    config_data = await REPOSITORY.get_config()
    budget_data = await REPOSITORY.get_budget()
    rebased = rebase_budget_snapshot(config_data, budget_data)

    changed = (
        money(budget_data["initial_budget"]) != money(rebased["initial_budget"])
        or money(budget_data["current_budget"]) != money(rebased["current_budget"])
        or money(budget_data["session_profit"]) != money(rebased["session_profit"])
    )
    if changed:
        budget_data = await REPOSITORY.save_budget(rebased)

    if update_state:
        await STATE.update(budget=budget_data)
    return budget_data
