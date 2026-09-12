from __future__ import annotations

import asyncio
import math
from typing import Any

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager

from . import forks_scanner as base_forks_scanner
from .forks_scanner import ForksTableTennisScanner
from .scanner import ScanAlreadyRunning, TableTennisScanError
from .state import TABLE_TENNIS_STATE, TableTennisStateStore, utc_now


class ArbitrageLocked(RuntimeError):
    """Internal control-flow signal: both paper legs are fixed and we can move on."""


def select_favorite_immediately(odd_1: float, odd_2: float) -> str:
    """Choose the lower odd immediately; exact ties deterministically use P1."""
    first = float(odd_1)
    second = float(odd_2)
    if not math.isfinite(first) or not math.isfinite(second) or first <= 0 or second <= 0:
        raise ValueError("Коэффициенты П1/П2 должны быть положительными числами.")
    return "p1" if first <= second else "p2"


# ForksTableTennisScanner imports select_favorite into its module namespace.
# Keep the proven Party-2 state machine, but make the first paper leg immediate
# even when P1 and P2 are equal (stable P1 tie-break instead of waiting).
base_forks_scanner.select_favorite = select_favorite_immediately


class SequentialForksTableTennisScanner(ForksTableTennisScanner):
    """One catalog scan -> one match at a time -> next match after a locked fork.

    The bookmaker page remains read-only. First/second legs, bankroll movement and
    closing profit are DEMO values only; no market button is clicked.
    """

    async def _close_locked_arbitrage(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Release DEMO reserve using the guaranteed result of an already locked fork.

        Once both mutually exclusive outcomes have been covered, the minimum of the
        two calculated profits is already known. Using that conservative amount lets
        the paper bankroll move to the next scanned match without inventing a winner
        or keeping money reserved until the party result is scraped later.
        """
        existing = payload.get("arb_settlement")
        if existing is not None:
            return existing

        first = payload.get("first_leg")
        second = payload.get("second_leg")
        fork = payload.get("fork")
        if not first or not second or not fork:
            raise RuntimeError("Нельзя закрыть DEMO-вилку без двух плеч и расчёта вилки.")

        balance_before = round(
            float(payload.get("balance_before_sequence", self._current_balance)),
            2,
        )
        total_invested = round(float(fork.get("total_stake", 0.0)), 2)
        guaranteed_profit = round(float(fork.get("guaranteed_profit", 0.0)), 2)
        balance_after = round(balance_before + guaranteed_profit, 2)

        self._current_balance = balance_after
        self._available_balance = balance_after
        self._reserved_balance = 0.0
        self._realized_profit = round(self._current_balance - self._starting_balance, 2)
        self._finished_series += 1
        if guaranteed_profit > 0:
            self._winning_series += 1
        elif guaranteed_profit < 0:
            self._losing_series += 1

        settled_at = utc_now()
        settlement = {
            "mode": "GUARANTEED_ARBITRAGE",
            "balance_before": balance_before,
            "total_invested": total_invested,
            "payout": round(total_invested + guaranteed_profit, 2),
            "balance_after": balance_after,
            "profit_loss": guaranteed_profit,
            "winner": None,
            "winning_leg": None,
        }
        payload.update(
            arb_settlement=settlement,
            settlement=settlement,
            winner=None,
            profit_loss=guaranteed_profit,
            balance_after=balance_after,
            available_balance=balance_after,
            reserved_balance=0.0,
            settled_at=settled_at,
            monitoring_status="ARB_LOCKED",
        )

        completed = {
            "sequence_id": payload.get("sequence_id"),
            "event_id": payload.get("event_id"),
            "league_name": payload.get("league_name"),
            "player_1": payload.get("player_1"),
            "player_2": payload.get("player_2"),
            "url": payload.get("url"),
            "party": self.TARGET_SET,
            "first_leg": first,
            "second_leg": second,
            "fork": fork,
            "arb_percent": payload.get("arbitrage_percent_preview"),
            "total_invested": total_invested,
            "winner": None,
            "profit_loss": guaranteed_profit,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "status": "ARB_LOCKED",
            "opened_at": first.get("placed_at"),
            "closed_at": settled_at,
            "settlement": settlement,
        }
        self._forks.insert(0, completed)
        self._forks = self._forks[:200]

        roi = (
            self._realized_profit / self._starting_balance * 100
            if self._starting_balance
            else 0.0
        )
        await self.state.update(
            fork_last=completed,
            forks_count=len(self._forks),
            fork_reserved=0.0,
            fork_available_budget=balance_after,
            current_balance=balance_after,
            available_balance=balance_after,
            reserved_balance=0.0,
            realized_profit=self._realized_profit,
            current_series_profit=guaranteed_profit,
            roi_percent=roi,
            completed_series=self._finished_series,
            winning_series=self._winning_series,
            losing_series=self._losing_series,
        )
        await self._publish_active(payload)
        await self._log(
            "ARB CLOSED",
            f"guaranteed pnl={guaranteed_profit:+.2f}; next match can start",
        )
        await self._log(
            "BANK",
            f"before={balance_before:.2f}; after={balance_after:.2f}; "
            f"reserved=0.00; pnl={guaranteed_profit:+.2f}",
        )
        return settlement

    async def _complete_second_leg(
        self,
        payload: dict[str, Any],
        *,
        odds: float,
    ) -> bool:
        # Idempotence: a retry after the control-flow signal must not duplicate
        # either the second leg or the history record.
        if payload.get("second_leg") is not None:
            return True

        completed = await super()._complete_second_leg(payload, odds=odds)
        if not completed:
            return False

        await self._close_locked_arbitrage(payload)
        # Abort the current-match monitor immediately. The runner catches this
        # signal and opens the next item from the already scanned queue.
        raise ArbitrageLocked

    async def _run_strategy(self) -> None:
        if self._scan_lock.locked():
            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")
        if self._budget <= 0 or self._initial_stake <= 0:
            raise TableTennisScanError("Перед запуском задайте бюджет и первоначальную ставку.")

        async with self._scan_lock:
            try:
                page, table_tennis_url = await self._authorize_page()

                # Critical invariant: exactly one catalog scan per bot start.
                # No background rescanning is performed while a selected match is
                # waiting for Party 2, odds, or the opposite hedge coefficient.
                candidates = await self._scan_zero_zero_catalog(page, table_tennis_url)
                if not candidates:
                    await self.state.update(
                        status="NO_LIVE_ZERO_ZERO_MATCH",
                        scanning=False,
                        active_match_id=None,
                        active_match=None,
                    )
                    await self._log(
                        "FORKS_NO_MATCH",
                        "Однократное сканирование не нашло подходящих матчей 0:0.",
                    )
                    return

                for candidate in candidates:
                    if self._stop_event.is_set():
                        break
                    if self._available_balance < self._initial_stake:
                        await self.state.update(status="INSUFFICIENT_BUDGET", scanning=False)
                        break

                    await self._log(
                        "FORKS_MATCH_SELECTED",
                        f"event={candidate.event_id}; {candidate.player_1} - "
                        f"{candidate.player_2}; score={candidate.score}",
                    )
                    try:
                        outcome = await self._observe_zero_zero(page, candidate)
                    except ArbitrageLocked:
                        outcome = "ARB_LOCKED"

                    if candidate.event_id:
                        self._processed_event_ids.add(str(candidate.event_id))
                    await self._log(
                        "FORKS_MATCH_FINISHED",
                        f"event={candidate.event_id}; outcome={outcome}",
                    )

                await self.state.update(scanning=False)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Unexpected errors stop the run instead of silently jumping to a
                # different match. Missing Party 2 itself is handled by the base
                # observation loop and is retried on the same event.
                await self._publish_error(error)


TABLE_TENNIS_SCANNER = SequentialForksTableTennisScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
