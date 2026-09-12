from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager

from .sequential_forks_scanner import SequentialForksTableTennisScanner
from .state import TABLE_TENNIS_STATE, TableTennisStateStore


_FIRST_LEG_STATE_KEYS = (
    "first_leg",
    "second_leg",
    "second_side",
    "first_bet_player",
    "first_bet_odd",
    "first_bet_amount",
    "opposite_player",
    "hedge_player",
    "sequence_id",
    "first_bet_wait_reason",
    "balance_before_sequence",
    "available_balance",
    "reserved_balance",
    "minimum_second_odds",
    "zero_fork_capital_required",
)


class IdempotentSequentialForksTableTennisScanner(SequentialForksTableTennisScanner):
    """Sequential Party-2 strategy with event-level idempotence for FIRST LEG.

    A DOM/SPA retry can restart `_observe_zero_zero()` for the same event. That
    method builds a fresh payload, so the inherited `_record_first_leg()` would
    see `first_leg=None` and reserve the initial stake again. The browser then
    still showed one paper bet, while the DEMO bank incorrectly showed two.

    Keep the accepted first leg outside the transient observation payload and
    restore it on retries. Only the first successful placement changes the bank.
    """

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager=browser_manager, state=state)
        self._first_leg_state_by_event: dict[str, dict[str, Any]] = {}

    async def configure(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        # A new explicit bot start is a new paper-trading session.
        self._first_leg_state_by_event.clear()
        return await super().configure(budget=budget, initial_stake=initial_stake)

    async def clear_forks(self) -> dict[str, Any]:
        self._first_leg_state_by_event.clear()
        return await super().clear_forks()

    async def _record_first_leg(
        self,
        payload: dict[str, Any],
        *,
        side: str,
        odds: float,
    ) -> None:
        event_id = str(payload.get("event_id") or "")
        cached = self._first_leg_state_by_event.get(event_id) if event_id else None

        if cached is not None:
            # Restore only strategy/bank fields. Current live odds and scoreboard
            # values from the new observation payload must remain current.
            for key in _FIRST_LEG_STATE_KEYS:
                if key in cached:
                    payload[key] = deepcopy(cached[key])

            payload["monitoring_status"] = "WAITING_FOR_ARB"
            await self._publish_active(payload)
            first = payload.get("first_leg") or {}
            await self._log(
                "FIRST LEG RESUME",
                f"event={event_id}; {str(first.get('side')).upper()} "
                f"@ {float(first.get('accepted_odd', first.get('odds', 0.0))):.3f}; "
                "bank reserve unchanged",
            )
            return

        available_before = self._available_balance
        reserved_before = self._reserved_balance
        await super()._record_first_leg(payload, side=side, odds=odds)

        first = payload.get("first_leg")
        if first is None or not event_id:
            return

        # Store the accepted paper leg only after the inherited method has
        # successfully reserved exactly the initial stake.
        self._first_leg_state_by_event[event_id] = {
            key: deepcopy(payload.get(key))
            for key in _FIRST_LEG_STATE_KEYS
            if key in payload
        }

        expected_available = round(available_before - self._initial_stake, 2)
        expected_reserved = round(reserved_before + self._initial_stake, 2)
        if (
            self._available_balance != expected_available
            or self._reserved_balance != expected_reserved
        ):
            await self._log(
                "BANK_GUARD",
                f"event={event_id}; expected available={expected_available:.2f}, "
                f"reserved={expected_reserved:.2f}; actual available="
                f"{self._available_balance:.2f}, reserved={self._reserved_balance:.2f}",
            )


TABLE_TENNIS_SCANNER = IdempotentSequentialForksTableTennisScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
