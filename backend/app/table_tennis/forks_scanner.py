from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from playwright.async_api import Page

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from xbet_config import get_table_tennis_odds_poll_interval

from .fork_math import (
    calculate_fork,
    minimum_second_odds,
    select_zero_zero_matches,
    zero_fork_capital_required,
    zero_zero_candidate_payload,
)
from .models import TableTennisMatch
from .monitoring import (
    StaleMatchError,
    TargetPartyNotAvailable,
    ensure_same_event,
    open_target_party,
    read_target_market_odds,
)
from .scanner import (
    ScanAlreadyRunning,
    TableTennisScanError,
    TableTennisScanner,
    deduplicate_match_models,
    scan_league_matches,
    scan_leagues,
)
from .selectors import LEAGUE_GROUP_SELECTOR, LEAGUE_LINK_SELECTOR
from .state import TABLE_TENNIS_STATE, TableTennisStateStore, utc_now


class ForksTableTennisScanner(TableTennisScanner):
    """Paper-trading table-tennis forks strategy.

    It scans every league, keeps only 0:0 matches, opens the nearest candidate,
    watches 1X2 of the first party and records two virtual legs. It never clicks
    bookmaker odds or sends a real bet.
    """

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager, state)
        self._budget = 0.0
        self._initial_stake = 0.0
        self._forks: list[dict[str, Any]] = []
        self._processed_event_ids: set[str] = set()

    async def configure(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        budget = float(budget)
        initial_stake = float(initial_stake)
        if budget <= 0:
            raise ValueError("Бюджет должен быть больше нуля.")
        if initial_stake <= 0:
            raise ValueError("Первоначальная ставка должна быть больше нуля.")
        if initial_stake > budget:
            raise ValueError("Первоначальная ставка не может быть больше бюджета.")
        self._budget = budget
        self._initial_stake = initial_stake
        await self.state.update(
            fork_budget=budget,
            fork_initial_stake=initial_stake,
            fork_reserved=0.0,
            fork_available_budget=budget,
        )
        return await self.state.snapshot()

    async def start_with_config(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        await self.configure(budget=budget, initial_stake=initial_stake)
        return await self.start()

    async def forks(self) -> list[dict[str, Any]]:
        return list(self._forks)

    async def clear_forks(self) -> dict[str, Any]:
        self._forks.clear()
        self._processed_event_ids.clear()
        await self.state.update(
            fork_reserved=0.0,
            fork_available_budget=self._budget,
            fork_last=None,
            forks_count=0,
        )
        return await self.state.snapshot()

    async def _scan_zero_zero_catalog(
        self,
        page: Page,
        table_tennis_url: str,
    ) -> list[TableTennisMatch]:
        await self.state.update(
            status="SCANNING_ZERO_ZERO",
            scanning=True,
            current_url=table_tennis_url,
            current_league=None,
            active_match_id=None,
            active_match=None,
            last_scan_started_at=utc_now(),
            error=None,
            league_errors=[],
        )
        await page.goto(table_tennis_url, wait_until="domcontentloaded", timeout=60_000)
        roots = page.locator(f"{LEAGUE_GROUP_SELECTOR}, {LEAGUE_LINK_SELECTOR}")
        await roots.first.wait_for(state="attached", timeout=20_000)
        leagues = await scan_leagues(page, table_tennis_url, self._log)
        scanned_leagues = list(leagues)
        collected: list[TableTennisMatch] = []
        errors: list[dict[str, str]] = []
        await self.state.update(leagues_found=len(leagues))

        for index, league in enumerate(leagues):
            if self._stop_event.is_set():
                raise asyncio.CancelledError
            await self.state.update(current_league=league.name, current_url=league.url)
            try:
                matches = await scan_league_matches(page, league, table_tennis_url)
                collected.extend(matches)
                scanned_leagues[index] = replace(league, parsed_games_count=len(matches))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append({"league": league.name, "error": f"{type(error).__name__}: {error}"})
                await self._log("FORKS_LEAGUE_ERROR", errors[-1]["error"])

        unique = deduplicate_match_models(collected)
        candidates = [
            match for match in select_zero_zero_matches(unique)
            if str(match.event_id) not in self._processed_event_ids
        ]
        await self.state.replace_results(
            [item.to_dict() for item in scanned_leagues],
            [item.to_dict() for item in candidates],
            [zero_zero_candidate_payload(item) for item in candidates],
        )
        await self.state.update(
            status="ZERO_ZERO_READY" if candidates else "WAITING_FOR_ZERO_ZERO",
            matches_found=len(candidates),
            candidates_count=len(candidates),
            current_league=None,
            current_url=page.url,
            last_scan_finished_at=utc_now(),
            league_errors=errors,
        )
        return candidates

    async def _publish_active(self, payload: dict[str, Any]) -> None:
        await self.state.update_candidate(payload)
        await self.state.set_active_match(payload)
        await self.state.update(
            status=payload.get("monitoring_status") or "MONITORING",
            scanning=True,
            current_url=payload.get("url"),
        )

    async def _record_first_leg(
        self,
        payload: dict[str, Any],
        *,
        side: str,
        odds: float,
    ) -> None:
        required = zero_fork_capital_required(self._initial_stake, odds)
        if required > self._budget:
            payload.update(
                monitoring_status="INSUFFICIENT_BUDGET",
                budget_required=required,
            )
            await self._publish_active(payload)
            raise RuntimeError(
                f"Для нулевой вилки при коэффициенте {odds} требуется минимум {required:.2f}, "
                f"доступно {self._budget:.2f}."
            )
        opposite = "p2" if side == "p1" else "p1"
        threshold = minimum_second_odds(odds)
        payload.update(
            first_leg={
                "side": side,
                "player": payload[f"player_{1 if side == 'p1' else 2}"],
                "odds": odds,
                "stake": self._initial_stake,
                "placed_at": utc_now(),
                "mode": "PAPER",
            },
            second_leg=None,
            second_side=opposite,
            minimum_second_odds=threshold,
            zero_fork_capital_required=required,
            monitoring_status="WAITING_SECOND_LEG",
        )
        await self.state.update(
            fork_reserved=self._initial_stake,
            fork_available_budget=max(0.0, self._budget - self._initial_stake),
        )
        await self._publish_active(payload)
        await self._log(
            "FORKS_FIRST_LEG_PAPER",
            f"side={side}; odds={odds}; stake={self._initial_stake}; second_min={threshold:.4f}",
        )

    async def _complete_second_leg(
        self,
        payload: dict[str, Any],
        *,
        odds: float,
    ) -> bool:
        first = payload.get("first_leg") or {}
        if not first:
            return False
        calc = calculate_fork(self._initial_stake, float(first["odds"]), odds)
        if not calc.profitable:
            payload.update(
                required_second_stake=calc.second_stake,
                fork_percent_preview=calc.fork_percent,
            )
            await self._publish_active(payload)
            return False
        if calc.total_stake > self._budget:
            payload.update(
                monitoring_status="INSUFFICIENT_BUDGET_SECOND_LEG",
                required_second_stake=calc.second_stake,
                budget_required=calc.total_stake,
            )
            await self._publish_active(payload)
            return False

        side = payload.get("second_side")
        payload.update(
            second_leg={
                "side": side,
                "player": payload[f"player_{1 if side == 'p1' else 2}"],
                "odds": odds,
                "stake": calc.second_stake,
                "placed_at": utc_now(),
                "mode": "PAPER",
            },
            required_second_stake=calc.second_stake,
            fork_percent_preview=calc.fork_percent,
            fork=calc.to_dict(),
            monitoring_status="FORK_COMPLETED",
            market_status="MARKET_AVAILABLE",
        )
        completed = {
            "event_id": payload.get("event_id"),
            "league_name": payload.get("league_name"),
            "player_1": payload.get("player_1"),
            "player_2": payload.get("player_2"),
            "url": payload.get("url"),
            "first_leg": payload["first_leg"],
            "second_leg": payload["second_leg"],
            "fork": payload["fork"],
            "completed_at": utc_now(),
        }
        self._forks.insert(0, completed)
        self._forks = self._forks[:200]
        await self.state.update_candidate(payload)
        await self.state.set_active_match(None)
        await self.state.update(
            status="FORK_COMPLETED",
            fork_last=completed,
            forks_count=len(self._forks),
            fork_reserved=0.0,
            fork_available_budget=self._budget,
        )
        await self._log(
            "FORKS_COMPLETED_PAPER",
            f"event={payload.get('event_id')}; profit={calc.guaranteed_profit}; percent={calc.fork_percent}",
        )
        return True

    async def _observe_zero_zero(self, page: Page, match: TableTennisMatch) -> str:
        payload = zero_zero_candidate_payload(match)
        payload["monitoring_status"] = "MATCH_SELECTED"
        await self._publish_active(payload)
        if not match.event_id or not match.url:
            return "INVALID_MATCH"

        try:
            await page.goto(match.url, wait_until="domcontentloaded", timeout=60_000)
            ensure_same_event(page, match.event_id)
            await open_target_party(page, match.event_id, 1, self._stop_event)
            payload["monitoring_status"] = "WAITING_ODDS_MOVEMENT"
            await self._publish_active(payload)

            initial: tuple[float, float] | None = None
            while not self._stop_event.is_set():
                ensure_same_event(page, match.event_id)
                market = await read_target_market_odds(page, match.event_id, 1)
                if market is None or not market.available:
                    payload["market_status"] = "MARKET_LOCKED"
                    await self._publish_active(payload)
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                    continue

                assert market.p1 is not None and market.p2 is not None
                payload.update(
                    market_status="MARKET_AVAILABLE",
                    current_odds_p1=market.p1,
                    current_odds_p2=market.p2,
                    odds_updated_at=utc_now(),
                )
                just_initialized = initial is None
                if just_initialized:
                    initial = (market.p1, market.p2)
                    payload.update(initial_odds_p1=market.p1, initial_odds_p2=market.p2)
                payload.setdefault("odds_history", []).append(
                    {"timestamp": utc_now(), "p1": market.p1, "p2": market.p2}
                )
                payload["odds_history"] = payload["odds_history"][-200:]

                if payload.get("first_leg") is None:
                    if just_initialized and market.p1 != market.p2:
                        side = "p1" if market.p1 > market.p2 else "p2"
                        odds = market.p1 if side == "p1" else market.p2
                        await self._record_first_leg(payload, side=side, odds=odds)
                    elif initial is not None:
                        delta1 = market.p1 - initial[0]
                        delta2 = market.p2 - initial[1]
                        if delta1 > 0 or delta2 > 0:
                            if delta1 == delta2:
                                side = "p1" if market.p1 >= market.p2 else "p2"
                            else:
                                side = "p1" if delta1 > delta2 else "p2"
                            odds = market.p1 if side == "p1" else market.p2
                            await self._record_first_leg(payload, side=side, odds=odds)
                else:
                    side = str(payload.get("second_side"))
                    odds = market.p1 if side == "p1" else market.p2
                    if odds > float(payload.get("minimum_second_odds") or 9999):
                        if await self._complete_second_leg(payload, odds=odds):
                            return "FORK_COMPLETED"
                    else:
                        try:
                            preview = calculate_fork(
                                self._initial_stake,
                                float(payload["first_leg"]["odds"]),
                                odds,
                            )
                            payload.update(
                                required_second_stake=preview.second_stake,
                                fork_percent_preview=preview.fork_percent,
                            )
                        except ValueError:
                            pass
                await self._publish_active(payload)
                await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
        except (TargetPartyNotAvailable, StaleMatchError) as error:
            payload["monitoring_status"] = type(error).__name__.upper()
            await self.state.update_candidate(payload)
            await self.state.set_active_match(None)
            return payload["monitoring_status"]
        return "STOPPED"

    async def _run_strategy(self) -> None:
        if self._scan_lock.locked():
            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")
        if self._budget <= 0 or self._initial_stake <= 0:
            raise TableTennisScanError("Перед запуском задайте бюджет и первоначальную ставку.")

        async with self._scan_lock:
            try:
                page, table_tennis_url = await self._authorize_page()
                while not self._stop_event.is_set():
                    candidates = await self._scan_zero_zero_catalog(page, table_tennis_url)
                    if not candidates:
                        await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                        continue
                    candidate = candidates[0]
                    outcome = await self._observe_zero_zero(page, candidate)
                    if candidate.event_id:
                        self._processed_event_ids.add(str(candidate.event_id))
                    await self._log("FORKS_MATCH_FINISHED", f"event={candidate.event_id}; outcome={outcome}")
                    await self._sleep_or_stop(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._publish_error(error)


TABLE_TENNIS_SCANNER = ForksTableTennisScanner()
