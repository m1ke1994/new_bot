from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

from playwright.async_api import Page

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from backend.app.browser.scoreboard import ScoreReadError, read_scoreboard
from xbet_config import (
    get_table_tennis_min_arb_percent,
    get_table_tennis_odds_poll_interval,
)

from .fork_math import (
    calculate_fork,
    calculate_series_settlement,
    minimum_second_odds,
    select_favorite,
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
    parse_party_number,
    read_current_party,
    read_selected_party,
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


def infer_second_set_winner(
    start_score: tuple[int, int] | None,
    last_score: tuple[int, int] | None,
    after_score: tuple[int, int] | None,
) -> str | None:
    """Support both cumulative-set and current-party scoreboard layouts."""
    if start_score is not None and after_score is not None:
        delta_1 = after_score[0] - start_score[0]
        delta_2 = after_score[1] - start_score[1]
        if delta_1 > 0 and delta_2 == 0:
            return "p1"
        if delta_2 > 0 and delta_1 == 0:
            return "p2"
    if (
        start_score is not None
        and last_score is not None
        and last_score != start_score
        and last_score[0] != last_score[1]
    ):
        return "p1" if last_score[0] > last_score[1] else "p2"
    return None


class ForksTableTennisScanner(TableTennisScanner):
    """Paper-trading table-tennis forks strategy.

    At startup it scans the table-tennis catalog exactly once, keeps only LIVE
    matches with score 0:0, opens the first such match, watches 1X2 of Party 2
    and records two virtual legs. It never clicks bookmaker odds or sends
    a real bet.
    """

    TARGET_SET = 2

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager, state)
        self._budget = 0.0
        self._starting_balance = 0.0
        self._current_balance = 0.0
        self._available_balance = 0.0
        self._reserved_balance = 0.0
        self._realized_profit = 0.0
        self._finished_series = 0
        self._winning_series = 0
        self._losing_series = 0
        self._initial_stake = 0.0
        self._forks: list[dict[str, Any]] = []
        self._processed_event_ids: set[str] = set()
        self._last_hedge_log: tuple[str, float, str] | None = None

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
        self._starting_balance = budget
        self._current_balance = budget
        self._available_balance = budget
        self._reserved_balance = 0.0
        self._realized_profit = 0.0
        self._finished_series = 0
        self._winning_series = 0
        self._losing_series = 0
        self._initial_stake = initial_stake
        await self.state.update(
            fork_budget=budget,
            fork_initial_budget=budget,
            fork_initial_stake=initial_stake,
            fork_first_stake=0.0,
            fork_second_stake=0.0,
            fork_reserved=0.0,
            fork_available_budget=budget,
            starting_balance=budget,
            current_balance=budget,
            available_balance=budget,
            reserved_balance=0.0,
            realized_profit=0.0,
            current_series_profit=None,
            roi_percent=0.0,
            completed_series=0,
            winning_series=0,
            losing_series=0,
            min_arb_percent=get_table_tennis_min_arb_percent(),
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
            fork_first_stake=0.0,
            fork_second_stake=0.0,
            fork_reserved=0.0,
            fork_available_budget=self._current_balance,
            available_balance=self._current_balance,
            reserved_balance=0.0,
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
            status="SEARCHING_MATCH",
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
            match
            for match in select_zero_zero_matches(unique)
            if match.status == "LIVE"
            and match.started is True
            and str(match.event_id) not in self._processed_event_ids
        ]
        await self.state.replace_results(
            [item.to_dict() for item in scanned_leagues],
            [item.to_dict() for item in candidates],
            [zero_zero_candidate_payload(item) for item in candidates],
        )
        await self.state.update(
            status="ZERO_ZERO_READY" if candidates else "NO_LIVE_ZERO_ZERO_MATCH",
            matches_found=len(candidates),
            candidates_count=len(candidates),
            current_league=None,
            current_url=page.url,
            last_scan_finished_at=utc_now(),
            league_errors=errors,
        )
        return candidates

    async def _publish_active(self, payload: dict[str, Any]) -> None:
        payload.update(
            league=payload.get("league_name"),
            player1=payload.get("player_1"),
            player2=payload.get("player_2"),
            player1_odd=payload.get("current_odds_p1"),
            player2_odd=payload.get("current_odds_p2"),
            status=payload.get("monitoring_status"),
        )
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
        if payload.get("first_leg") is not None:
            return
        if self._initial_stake > self._available_balance:
            payload.update(
                monitoring_status="INSUFFICIENT_BUDGET",
                budget_required=self._initial_stake,
            )
            await self._publish_active(payload)
            return
        opposite = "p2" if side == "p1" else "p1"
        threshold = minimum_second_odds(odds)
        required = zero_fork_capital_required(self._initial_stake, odds)
        sequence_id = f"{payload.get('event_id') or 'event'}-set-{self.TARGET_SET}-{uuid4().hex}"
        first_leg = {
            "side": side,
            "player": payload[f"player_{1 if side == 'p1' else 2}"],
            "odds": odds,
            "accepted_odd": odds,
            "stake": self._initial_stake,
            "placed_at": utc_now(),
            "mode": "PAPER",
            "sequence_id": sequence_id,
        }
        balance_before = self._current_balance
        self._available_balance = round(self._available_balance - self._initial_stake, 2)
        self._reserved_balance = round(self._reserved_balance + self._initial_stake, 2)
        payload.update(
            first_leg=first_leg,
            second_leg=None,
            second_side=opposite,
            first_bet_player=side,
            first_bet_odd=odds,
            first_bet_amount=self._initial_stake,
            opposite_player=opposite,
            hedge_player=opposite,
            sequence_id=sequence_id,
            first_bet_wait_reason=None,
            balance_before_sequence=balance_before,
            available_balance=self._available_balance,
            reserved_balance=self._reserved_balance,
            minimum_second_odds=threshold,
            zero_fork_capital_required=required,
            monitoring_status="FIRST_BET_PLACED",
        )
        await self.state.update(
            fork_first_stake=self._initial_stake,
            fork_second_stake=0.0,
            fork_reserved=self._reserved_balance,
            fork_available_budget=self._available_balance,
            current_balance=self._current_balance,
            available_balance=self._available_balance,
            reserved_balance=self._reserved_balance,
            current_series_profit=None,
        )
        await self._publish_active(payload)
        await self._log(
            "FIRST LEG",
            f"{side.upper()} selected; odd={odds:.3f}; stake={self._initial_stake:.2f}",
        )
        await self._log(
            "HEDGE",
            f"Watching {opposite.upper()}; zero_odd={threshold:.3f}; "
            f"min_arb={get_table_tennis_min_arb_percent():.2f}%",
        )
        await self._log(
            "BANK",
            f"before={balance_before:.2f}; available={self._available_balance:.2f}; "
            f"reserved={self._reserved_balance:.2f}",
        )
        payload["monitoring_status"] = "WAITING_FOR_ARB"
        await self._publish_active(payload)

    async def _complete_second_leg(
        self,
        payload: dict[str, Any],
        *,
        odds: float,
    ) -> bool:
        if payload.get("second_leg") is not None:
            return True
        first = payload.get("first_leg") or {}
        if not first:
            return False
        accepted_odd = float(first.get("accepted_odd", first["odds"]))
        calc = calculate_fork(
            float(first["stake"]),
            accepted_odd,
            odds,
            min_arb_percent=get_table_tennis_min_arb_percent(),
        )
        if not calc.profitable:
            payload.update(
                required_second_stake=calc.second_stake,
                fork_percent_preview=calc.arbitrage_percent,
                arbitrage_percent_preview=calc.arbitrage_percent,
                monitoring_status="WAITING_FOR_ARB",
            )
            await self._publish_active(payload)
            return False
        available_budget = self._available_balance
        if calc.second_stake > available_budget:
            payload.update(
                monitoring_status="INSUFFICIENT_BUDGET_SECOND_LEG",
                required_second_stake=calc.second_stake,
                budget_required=calc.total_stake,
            )
            await self._publish_active(payload)
            return False

        side = payload.get("second_side")
        sequence_id = payload.get("sequence_id")
        second_leg = {
            "side": side,
            "player": payload[f"player_{1 if side == 'p1' else 2}"],
            "odds": odds,
            "accepted_odd": odds,
            "stake": calc.second_stake,
            "placed_at": utc_now(),
            "mode": "PAPER",
            "sequence_id": sequence_id,
        }
        payload.update(
            second_leg=second_leg,
            required_second_stake=calc.second_stake,
            fork_percent_preview=calc.arbitrage_percent,
            arbitrage_percent_preview=calc.arbitrage_percent,
            fork=calc.to_dict(),
            monitoring_status="SECOND_BET_PLACED",
            market_status="MARKET_AVAILABLE",
        )
        self._available_balance = round(self._available_balance - calc.second_stake, 2)
        self._reserved_balance = round(self._reserved_balance + calc.second_stake, 2)
        payload.update(
            available_balance=self._available_balance,
            reserved_balance=self._reserved_balance,
        )
        await self.state.update(
            fork_second_stake=calc.second_stake,
            fork_reserved=self._reserved_balance,
            fork_available_budget=self._available_balance,
            current_balance=self._current_balance,
            available_balance=self._available_balance,
            reserved_balance=self._reserved_balance,
        )
        await self._publish_active(payload)
        await self._log(
            "SECOND LEG",
            f"{str(side).upper()}; odd={odds:.3f}; stake={calc.second_stake:.2f}",
        )
        payload["monitoring_status"] = "ARB_LOCKED"
        await self._publish_active(payload)
        await self._log(
            "ARB CLOSED",
            f"profit first={calc.profit_if_first:.2f}; profit second={calc.profit_if_second:.2f}; "
            f"arb={calc.arbitrage_percent:.2f}%",
        )
        await self._log(
            "BANK",
            f"available={self._available_balance:.2f}; reserved={self._reserved_balance:.2f}",
        )
        payload["monitoring_status"] = "WAITING_RESULT"
        await self._publish_active(payload)
        return True

    async def _read_second_set_winner(
        self,
        page: Page,
        payload: dict[str, Any],
    ) -> str | None:
        """Track Party 2 through the existing scoreboard parser and infer its winner."""
        try:
            current_set = await read_current_party(page)
        except Exception:
            current_set = None

        snapshot = None
        try:
            snapshot = await read_scoreboard(page)
        except ScoreReadError:
            pass

        score: tuple[int, int] | None = None
        finished_marker = False
        if snapshot is not None:
            score = (snapshot.score.team1, snapshot.score.team2)
            if current_set is None:
                current_set = parse_party_number(snapshot.period)
            payload.update(
                player_1=snapshot.team1 or payload.get("player_1"),
                player_2=snapshot.team2 or payload.get("player_2"),
                current_score=f"{score[0]}:{score[1]}",
                current_set=current_set,
            )
            normalized_period = (snapshot.period or "").casefold()
            finished_marker = any(
                marker in normalized_period
                for marker in ("заверш", "окончен", "finished", "full time")
            )

        if current_set == self.TARGET_SET and score is not None:
            payload.setdefault("second_set_start_score", list(score))
            payload["second_set_last_score"] = list(score)
            return None

        target_finished = (
            current_set is not None and current_set > self.TARGET_SET
        ) or finished_marker
        if not target_finished:
            return None

        start = payload.get("second_set_start_score")
        last = payload.get("second_set_last_score")
        return infer_second_set_winner(
            tuple(start) if start is not None else None,
            tuple(last) if last is not None else None,
            score,
        )

    async def _settle_series(
        self,
        payload: dict[str, Any],
        winner: str,
    ) -> dict[str, Any] | None:
        if payload.get("settled_at") is not None:
            return payload.get("settlement")
        first = payload.get("first_leg")
        if first is None:
            payload.update(
                winner=f"PLAYER_{1 if winner == 'p1' else 2}_WIN",
                monitoring_status="FINISHED",
                settled_at=utc_now(),
            )
            await self._publish_active(payload)
            return None

        second = payload.get("second_leg")
        result = calculate_series_settlement(
            float(payload.get("balance_before_sequence", self._current_balance)),
            str(first["side"]),
            float(first["stake"]),
            float(first.get("accepted_odd", first["odds"])),
            winner,
            hedge_side=str(second["side"]) if second else None,
            hedge_stake=float(second["stake"]) if second else None,
            hedge_odds=float(second.get("accepted_odd", second["odds"])) if second else None,
        )
        self._current_balance = result.balance_after
        self._available_balance = result.balance_after
        self._reserved_balance = 0.0
        self._realized_profit = round(self._current_balance - self._starting_balance, 2)
        self._finished_series += 1
        if result.profit_loss > 0:
            self._winning_series += 1
        elif result.profit_loss < 0:
            self._losing_series += 1

        history_status = "CLOSED" if second is not None else "HEDGE_NOT_FOUND"
        settled_at = utc_now()
        payload.update(
            winner=f"PLAYER_{1 if winner == 'p1' else 2}_WIN",
            settlement=result.to_dict(),
            profit_loss=result.profit_loss,
            balance_after=result.balance_after,
            available_balance=self._available_balance,
            reserved_balance=0.0,
            settled_at=settled_at,
            monitoring_status=("FINISHED" if second is not None else "HEDGE_NOT_FOUND"),
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
            "fork": payload.get("fork"),
            "arb_percent": payload.get("arbitrage_percent_preview"),
            "total_invested": result.total_invested,
            "winner": payload["winner"],
            "profit_loss": result.profit_loss,
            "balance_before": result.balance_before,
            "balance_after": result.balance_after,
            "status": history_status,
            "opened_at": first.get("placed_at"),
            "closed_at": settled_at,
            "settlement": result.to_dict(),
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
            fork_available_budget=self._available_balance,
            current_balance=self._current_balance,
            available_balance=self._available_balance,
            reserved_balance=0.0,
            realized_profit=self._realized_profit,
            current_series_profit=result.profit_loss,
            roi_percent=roi,
            completed_series=self._finished_series,
            winning_series=self._winning_series,
            losing_series=self._losing_series,
        )
        await self._publish_active(payload)
        await self._log("RESULT", payload["winner"])
        await self._log(
            "BANK",
            f"before={result.balance_before:.2f}; after={result.balance_after:.2f}; "
            f"pnl={result.profit_loss:+.2f}",
        )
        return result.to_dict()

    async def _observe_zero_zero(self, page: Page, match: TableTennisMatch) -> str:
        payload = zero_zero_candidate_payload(match)
        payload.update(
            target_set=self.TARGET_SET,
            party=self.TARGET_SET,
            monitoring_status="OPENING_MATCH",
        )
        await self._publish_active(payload)
        if not match.event_id or not match.url:
            return "INVALID_MATCH"

        try:
            await page.goto(match.url, wait_until="domcontentloaded", timeout=60_000)
            ensure_same_event(page, match.event_id)
            try:
                opened = await read_scoreboard(page)
                payload.update(
                    player_1=opened.team1,
                    player_2=opened.team2,
                    current_score=opened.score.text(),
                )
            except ScoreReadError:
                pass
            await self._publish_active(payload)
            await self._log(
                "ACTIVE MATCH",
                f"{payload.get('player_1')} vs {payload.get('player_2')}",
            )
            await self._log("SET", "Opening Party 2")
            while not self._stop_event.is_set():
                try:
                    await open_target_party(
                        page,
                        match.event_id,
                        self.TARGET_SET,
                        self._stop_event,
                        timeout=2.0,
                    )
                    break
                except TargetPartyNotAvailable:
                    payload.update(
                        monitoring_status="WAITING_SECOND_SET_MARKET",
                        market_status="WAITING_FOR_MARKET",
                    )
                    await self._publish_active(payload)
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())

            if self._stop_event.is_set():
                return "STOPPED"
            if await read_selected_party(page) != self.TARGET_SET:
                raise TargetPartyNotAvailable("Партия 2 не стала выбранной.")

            payload["monitoring_status"] = "SECOND_SET_SELECTED"
            await self._publish_active(payload)
            await self._log("SET", "Party 2 selected")
            payload["monitoring_status"] = "WAITING_SECOND_SET_MARKET"
            await self._publish_active(payload)

            while not self._stop_event.is_set():
                ensure_same_event(page, match.event_id)
                winner = await self._read_second_set_winner(page, payload)
                if winner is not None:
                    had_hedge = payload.get("second_leg") is not None
                    await self._settle_series(payload, winner)
                    return "FINISHED" if had_hedge or payload.get("first_leg") is None else "HEDGE_NOT_FOUND"

                if payload.get("second_leg") is not None:
                    if payload.get("monitoring_status") != "WAITING_RESULT":
                        payload["monitoring_status"] = "WAITING_RESULT"
                        await self._publish_active(payload)
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                    continue

                if await read_selected_party(page) != self.TARGET_SET:
                    payload["monitoring_status"] = "WAITING_SECOND_SET_MARKET"
                    await self._publish_active(payload)
                    try:
                        await open_target_party(
                            page,
                            match.event_id,
                            self.TARGET_SET,
                            self._stop_event,
                            timeout=2.0,
                        )
                    except TargetPartyNotAvailable:
                        await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                        continue
                    payload["monitoring_status"] = (
                        "WAITING_FOR_ARB" if payload.get("first_leg") else "WAITING_SECOND_SET_MARKET"
                    )
                    await self._publish_active(payload)

                market = await read_target_market_odds(
                    page,
                    match.event_id,
                    self.TARGET_SET,
                )
                if market is None or not market.available:
                    changed = payload.get("market_status") != "MARKET_LOCKED"
                    payload.update(
                        market_status="MARKET_LOCKED",
                        market_available=False,
                        monitoring_status="MARKET_LOCKED",
                        current_odds_p1=None,
                        current_odds_p2=None,
                    )
                    if changed:
                        await self._publish_active(payload)
                        await self._log("MARKET", "Party 2 odds locked; waiting for restore")
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                    continue

                assert market.p1 is not None and market.p2 is not None
                previous_market_status = payload.get("market_status")
                previous_monitoring_status = payload.get("monitoring_status")
                previous_odds = (
                    payload.get("current_odds_p1"),
                    payload.get("current_odds_p2"),
                )
                payload.update(
                    market_status="MARKET_AVAILABLE",
                    market=market.title,
                    market_available=True,
                    monitoring_status=(
                        "WAITING_FOR_ARB" if payload.get("first_leg") else "WAITING_ODDS_DIVERGENCE"
                    ),
                    current_odds_p1=market.p1,
                    current_odds_p2=market.p2,
                    odds_updated_at=utc_now(),
                )
                if payload.get("initial_odds_p1") is None:
                    payload.update(initial_odds_p1=market.p1, initial_odds_p2=market.p2)
                    await self._log("MARKET", f"Found: {market.title}")
                if previous_odds != (market.p1, market.p2):
                    payload.setdefault("odds_history", []).append(
                        {"timestamp": utc_now(), "p1": market.p1, "p2": market.p2}
                    )
                    payload["odds_history"] = payload["odds_history"][-200:]
                    await self._log(
                        "ODDS",
                        f"PLAYER_1={market.p1:.3f}; PLAYER_2={market.p2:.3f}",
                    )
                elif previous_market_status == "MARKET_LOCKED":
                    await self._log("MARKET", "Party 2 odds restored")

                if payload.get("first_leg") is None:
                    try:
                        side = select_favorite(market.p1, market.p2)
                        odds = market.p1 if side == "p1" else market.p2
                        await self._record_first_leg(payload, side=side, odds=odds)
                    except ValueError:
                        if previous_monitoring_status != "WAITING_ODDS_DIVERGENCE":
                            await self._log(
                                "FIRST LEG",
                                f"Odds equal ({market.p1:.3f} / {market.p2:.3f}); waiting for divergence",
                            )
                        payload.update(
                            monitoring_status="WAITING_ODDS_DIVERGENCE",
                            first_bet_wait_reason=(
                                f"Коэффициенты равны {market.p1:.3f} / {market.p2:.3f}"
                            ),
                        )
                else:
                    side = str(payload.get("second_side"))
                    odds = market.p1 if side == "p1" else market.p2
                    first = payload["first_leg"]
                    preview = calculate_fork(
                        float(first["stake"]),
                        float(first.get("accepted_odd", first["odds"])),
                        odds,
                        min_arb_percent=get_table_tennis_min_arb_percent(),
                    )
                    payload.update(
                        current_hedge_odd=odds,
                        required_second_stake=preview.second_stake,
                        fork_percent_preview=preview.arbitrage_percent,
                        arbitrage_percent_preview=preview.arbitrage_percent,
                        profit_if_first_preview=preview.profit_if_first,
                        profit_if_second_preview=preview.profit_if_second,
                    )
                    signature = (side, odds, payload["monitoring_status"])
                    if self._last_hedge_log != signature:
                        await self._log(
                            "HEDGE",
                            f"{side.upper()} current odd={odds:.3f}; "
                            f"arb={preview.arbitrage_percent:.2f}%; "
                            f"{'ARB FOUND' if preview.profitable else 'waiting...'}",
                        )
                        self._last_hedge_log = signature
                    if preview.profitable:
                        payload["monitoring_status"] = "ARB_FOUND"
                        await self._publish_active(payload)
                        await self._complete_second_leg(payload, odds=odds)
                await self._publish_active(payload)
                await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
        except (TargetPartyNotAvailable, StaleMatchError) as error:
            payload["monitoring_status"] = "ERROR"
            payload["error"] = str(error)
            await self.state.update_candidate(payload)
            await self.state.set_active_match(payload)
            return "ERROR"
        return "STOPPED"

    async def _run_strategy(self) -> None:
        if self._scan_lock.locked():
            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")
        if self._budget <= 0 or self._initial_stake <= 0:
            raise TableTennisScanError("Перед запуском задайте бюджет и первоначальную ставку.")

        async with self._scan_lock:
            try:
                page, table_tennis_url = await self._authorize_page()

                # Важно: каталог сканируется только один раз на один запуск бота.
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
                        "После однократного сканирования нет LIVE-матча со счётом 0:0.",
                    )
                    return

                # Обрабатываем найденную очередь последовательно; банк переносится
                # между сериями и никогда не сбрасывается после отдельного матча.
                for candidate in candidates:
                    if self._stop_event.is_set():
                        break
                    if self._available_balance < self._initial_stake:
                        await self.state.update(status="INSUFFICIENT_BUDGET", scanning=False)
                        break
                    await self._log(
                        "FORKS_MATCH_SELECTED",
                        f"event={candidate.event_id}; {candidate.player_1} - {candidate.player_2}; score={candidate.score}",
                    )
                    outcome = await self._observe_zero_zero(page, candidate)
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
                await self._publish_error(error)


TABLE_TENNIS_SCANNER = ForksTableTennisScanner()
