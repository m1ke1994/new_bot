from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from typing import Any

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager

from . import sequential_forks_scanner as sequential_module
from .monitoring import TargetPartyNotAvailable
from .sequential_forks_scanner import (
    SequentialForksTableTennisScanner,
    ensure_pinned_event,
)
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

_PARTY_ITEM_SELECTOR = ".game-sub-games__list .game-sub-games__item"
_PARTY_CAPTION_SELECTOR = ".ui-caption"
_SELECTED_PARTY_SELECTOR = (
    ".game-sub-games__list .game-sub-games__item.game-sub-games__item--is-selected, "
    ".game-sub-games__list .game-sub-games__item.game-sub-games__item--active, "
    ".game-sub-games__list .game-sub-games__item[aria-selected=\"true\"]"
)
_PARTY_RE = re.compile(r"^\s*(\d+)\s*[-–—]?\s*(?:я|й|ая)?\s*партия\s*$", re.IGNORECASE)


# Keep references before patching the sequential module globals.
_ORIGINAL_OPEN_TARGET_PARTY = sequential_module.open_target_party
_ORIGINAL_READ_PARTY_TWO_MARKET = sequential_module.read_party_two_1x2_odds_direct


def _normalize(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _party_number(value: str | None) -> int | None:
    match = _PARTY_RE.fullmatch(_normalize(value))
    return int(match.group(1)) if match else None


async def _caption_text(item: Any) -> str | None:
    try:
        caption = item.locator(_PARTY_CAPTION_SELECTOR).first
        if await caption.count() == 0:
            return None
        value = _normalize(await caption.inner_text())
        return value or None
    except Exception:
        return None


async def find_party_item_exact(page: Any, target_set: int = 2) -> Any | None:
    """Find the visible Party-N LI from the exact DOM supplied by 1xBet."""
    items = page.locator(_PARTY_ITEM_SELECTOR)
    for index in range(await items.count()):
        item = items.nth(index)
        if _party_number(await _caption_text(item)) == target_set:
            return item
    return None


async def read_selected_party_exact(page: Any) -> int | None:
    """Read the selected LI caption; «Основная игра» deliberately returns None."""
    selected = page.locator(_SELECTED_PARTY_SELECTOR)
    for index in range(await selected.count()):
        number = _party_number(await _caption_text(selected.nth(index)))
        if number is not None:
            return number
    return None


async def open_target_party_exact(
    page: Any,
    event_id: str,
    target_set: int,
    stop_event: asyncio.Event,
    *,
    timeout: float = 15.0,
) -> None:
    """Force-select Party 2 before any odds are read.

    The page opens on «Основная игра». We locate the exact
    `.game-sub-games__list .game-sub-games__item` whose `.ui-caption` is
    «2-я Партия», click the LI itself first, and verify that this same LI receives
    `game-sub-games__item--is-selected`. If Party 2 has not appeared yet, we stay
    on the same event and wait for it instead of reading hidden/stale markets.
    """
    if target_set != 2:
        await _ORIGINAL_OPEN_TARGET_PARTY(
            page,
            event_id,
            target_set,
            stop_event,
            timeout=timeout,
        )
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error: Exception | None = None

    while loop.time() < deadline and not stop_event.is_set():
        ensure_pinned_event(page, event_id)
        if await read_selected_party_exact(page) == target_set:
            return

        item = await find_party_item_exact(page, target_set)
        if item is None:
            await asyncio.sleep(0.10)
            continue

        # The current 1xBet DOM attaches selection to the LI. Click that exact
        # element first. Caption click and dispatch are fallbacks for re-renders.
        targets = [item, item.locator(_PARTY_CAPTION_SELECTOR).first]
        for target in targets:
            try:
                ensure_pinned_event(page, event_id)
                await target.scroll_into_view_if_needed()
                await target.click(timeout=2_000)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
                continue

            for _ in range(8):
                if stop_event.is_set():
                    raise asyncio.CancelledError
                ensure_pinned_event(page, event_id)
                if await read_selected_party_exact(page) == target_set:
                    return
                await asyncio.sleep(0.10)

        try:
            item = await find_party_item_exact(page, target_set)
            if item is not None:
                await item.dispatch_event("click")
                for _ in range(8):
                    if stop_event.is_set():
                        raise asyncio.CancelledError
                    if await read_selected_party_exact(page) == target_set:
                        return
                    await asyncio.sleep(0.10)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = error

        await asyncio.sleep(0.10)

    if stop_event.is_set():
        raise asyncio.CancelledError

    message = "Вкладка «2-я Партия» ещё не выбрана; остаёмся на текущем матче."
    if last_error is not None:
        message += f" Последняя ошибка: {type(last_error).__name__}: {last_error}"
    raise TargetPartyNotAvailable(message)


async def read_party_two_market_only_when_selected(
    page: Any,
    event_id: str,
):
    """Never read Party-2 P1/P2 while the UI is still on «Основная игра»."""
    ensure_pinned_event(page, event_id)
    if await read_selected_party_exact(page) != 2:
        return None
    return await _ORIGINAL_READ_PARTY_TWO_MARKET(page, event_id)


# SequentialForksTableTennisScanner resolves these helpers from module globals at
# runtime. Patching here guarantees the route's singleton uses the strict flow:
# open match -> click «2-я Партия» -> verify selection -> read P1/P2.
sequential_module.read_selected_party = read_selected_party_exact
sequential_module.open_target_party = open_target_party_exact
sequential_module.read_party_two_1x2_odds_direct = read_party_two_market_only_when_selected


class IdempotentSequentialForksTableTennisScanner(SequentialForksTableTennisScanner):
    """Sequential Party-2 strategy with idempotent first leg and live history."""

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager=browser_manager, state=state)
        self._first_leg_state_by_event: dict[str, dict[str, Any]] = {}

    async def configure(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        # A new explicit bot start is a new active strategy session. Historical
        # rows remain until the user presses «Очистить историю».
        self._first_leg_state_by_event.clear()
        return await super().configure(budget=budget, initial_stake=initial_stake)

    async def clear_forks(self) -> dict[str, Any]:
        self._first_leg_state_by_event.clear()
        return await super().clear_forks()

    def _history_entry(self, payload: dict[str, Any], *, status: str) -> dict[str, Any]:
        first = deepcopy(payload.get("first_leg"))
        second = deepcopy(payload.get("second_leg"))
        first_stake = float(first.get("stake", 0.0)) if first else 0.0
        second_stake = float(second.get("stake", 0.0)) if second else 0.0
        return {
            "sequence_id": payload.get("sequence_id"),
            "event_id": payload.get("event_id"),
            "league_name": payload.get("league_name"),
            "player_1": payload.get("player_1"),
            "player_2": payload.get("player_2"),
            "url": payload.get("url"),
            "party": self.TARGET_SET,
            "first_leg": first,
            "second_leg": second,
            "fork": deepcopy(payload.get("fork")),
            "arb_percent": payload.get("arbitrage_percent_preview"),
            "total_invested": round(first_stake + second_stake, 2),
            "winner": payload.get("winner"),
            "profit_loss": payload.get("profit_loss"),
            "balance_before": payload.get("balance_before_sequence"),
            "balance_after": payload.get("balance_after"),
            "status": status,
            "opened_at": first.get("placed_at") if first else None,
            "closed_at": payload.get("settled_at"),
        }

    async def _upsert_history(self, payload: dict[str, Any], *, status: str) -> None:
        sequence_id = payload.get("sequence_id")
        if not sequence_id or payload.get("first_leg") is None:
            return
        entry = self._history_entry(payload, status=status)
        for index, current in enumerate(self._forks):
            if current.get("sequence_id") == sequence_id:
                self._forks[index] = entry
                break
        else:
            self._forks.insert(0, entry)
            self._forks = self._forks[:200]
        await self.state.update(
            fork_last=entry,
            forks_count=len(self._forks),
        )

    def _remove_open_history_row(self, payload: dict[str, Any]) -> None:
        sequence_id = payload.get("sequence_id")
        if not sequence_id:
            return
        self._forks = [
            item for item in self._forks
            if item.get("sequence_id") != sequence_id
        ]

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
            await self._upsert_history(payload, status="FIRST_LEG_OPEN")
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

        # FIRST LEG must appear in /api/table-tennis/forks immediately, while the
        # opposite coefficient is still being monitored. The same row is later
        # replaced by the completed two-leg series instead of creating a duplicate.
        await self._upsert_history(payload, status="FIRST_LEG_OPEN")

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

    async def _close_locked_arbitrage(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Sequential parent inserts the finished row. Remove the already visible
        # FIRST_LEG_OPEN row first so history contains exactly one row per series.
        self._remove_open_history_row(payload)
        return await super()._close_locked_arbitrage(payload)

    async def _settle_series(
        self,
        payload: dict[str, Any],
        winner: str,
    ) -> dict[str, Any] | None:
        # Same de-duplication for a Party-2 finish before the hedge appeared.
        self._remove_open_history_row(payload)
        return await super()._settle_series(payload, winner)


TABLE_TENNIS_SCANNER = IdempotentSequentialForksTableTennisScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
