from __future__ import annotations

import asyncio
import math
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager
from backend.app.browser.scoreboard import ScoreReadError, read_scoreboard
from xbet_config import get_table_tennis_odds_poll_interval

from . import forks_scanner as base_forks_scanner
from . import monitoring as monitoring_module
from .fork_math import (
    calculate_fork,
    is_zero_zero_match,
    minimum_second_odds,
    zero_zero_candidate_payload,
)
from .forks_scanner import ForksTableTennisScanner
from .models import TableTennisMatch
from .monitoring import (
    StaleMatchError,
    TargetMarketOdds,
    TargetPartyNotAvailable,
    normalize_text,
    open_target_party,
    read_selected_party,
)
from .scanner import (
    ScanAlreadyRunning,
    TableTennisScanError,
    deduplicate_match_models,
    scan_league_matches,
    scan_leagues,
)
from .selectors import LEAGUE_GROUP_SELECTOR, LEAGUE_LINK_SELECTOR
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


def event_url_matches(url: str | None, expected_event_id: str | None) -> bool:
    """Accept the same event even when 1xBet appends a Party/sub-game route.

    The previous implementation required the whole path to match the strict event
    regex. 1xBet can change the SPA path after selecting a party, which made the
    same event look stale and restarted the observation loop over and over.
    """
    if not url or not expected_event_id:
        return False
    path = urlparse(str(url)).path
    event_id = re.escape(str(expected_event_id))
    return re.search(rf"(?:^|/){event_id}(?:-|/|$)", path) is not None


def ensure_pinned_event(page: Any, expected_event_id: str) -> None:
    if event_url_matches(getattr(page, "url", None), expected_event_id):
        return
    actual = monitoring_module.page_event_id(page)
    if actual == expected_event_id:
        return
    raise StaleMatchError(
        f"Ожидался event_id={expected_event_id}, открыт event_id={actual or 'UNKNOWN'}; "
        f"url={getattr(page, 'url', None) or 'UNKNOWN'}."
    )


def select_zero_zero_in_scan_order(
    matches: list[TableTennisMatch],
    processed_event_ids: set[str] | None = None,
) -> list[TableTennisMatch]:
    """Keep exact league/card DOM order; never re-sort candidates by clock time."""
    processed = processed_event_ids or set()
    return [
        match
        for match in matches
        if is_zero_zero_match(match)
        and str(match.event_id) not in processed
    ]


async def _node_text(node: Any, selector: str) -> str | None:
    try:
        locator = node.locator(selector).first
        if await locator.count() == 0:
            return None
        value = normalize_text(await locator.inner_text())
        return value or None
    except Exception:
        return None


async def read_party_two_1x2_odds_direct(
    page: Any,
    event_id: str,
) -> TargetMarketOdds | None:
    """Read P1/P2 strictly inside the parent block «1X2. 2-я Партия».

    This intentionally follows the DOM supplied by the site:
    .game-markets-content__item -> header title -> buttons ->
    .ui-market__name / .ui-market__value. It does not read neighboring totals.
    """
    ensure_pinned_event(page, event_id)
    group_selectors = (
        ".game-markets-content__item.game-markets-group, "
        ".game-markets-content__item"
    )
    groups = page.locator(group_selectors)
    matched_group = None

    for index in range(await groups.count()):
        group = groups.nth(index)
        title = await _node_text(
            group,
            ".game-markets-group-header-title .ui-caption, "
            ".game-markets-group-header-title",
        )
        if not title:
            continue
        normalized = title.casefold().replace("х", "x")
        if "1x2" in normalized and re.search(r"\b2\s*[-–—]?\s*я\s+партия\b", normalized):
            matched_group = group
            break

    if matched_group is None:
        # Fallback for a layout where the same accordion loses the
        # game-markets-content__item class but keeps game-markets-group.
        groups = page.locator(".game-markets-group")
        for index in range(await groups.count()):
            group = groups.nth(index)
            title = await _node_text(
                group,
                ".game-markets-group-header-title .ui-caption, "
                ".game-markets-group-header-title",
            )
            if not title:
                continue
            normalized = title.casefold().replace("х", "x")
            if "1x2" in normalized and re.search(r"\b2\s*[-–—]?\s*я\s+партия\b", normalized):
                matched_group = group
                break

    if matched_group is None:
        return None

    buttons = matched_group.locator(
        "button.game-markets-group__market, button.market, .game-markets-group__market"
    )
    if await buttons.count() == 0:
        header = matched_group.locator(".game-markets-group__header").first
        try:
            if await header.count():
                await header.click(timeout=2_000)
                await asyncio.sleep(0.05)
                buttons = matched_group.locator(
                    "button.game-markets-group__market, button.market, .game-markets-group__market"
                )
        except Exception:
            pass

    values: dict[str, float] = {}
    market_locked = False
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = await _node_text(button, ".ui-market__name")
        value = await _node_text(button, ".ui-market__value")
        if not name:
            continue
        key = name.casefold().replace(" ", "")
        if key not in {"п1", "п2"}:
            continue
        try:
            classes = (await button.get_attribute("class") or "").casefold()
            disabled = await button.is_disabled()
        except Exception:
            classes = ""
            disabled = False
        if disabled or "locked" in classes or "disabled" in classes or not value:
            market_locked = True
            continue
        raw = value.replace(",", ".")
        if re.fullmatch(r"\d+(?:\.\d+)?", raw) is None:
            market_locked = True
            continue
        parsed = float(raw)
        if parsed <= 1.0:
            market_locked = True
            continue
        values[key] = parsed

    ensure_pinned_event(page, event_id)
    if "п1" in values and "п2" in values:
        return TargetMarketOdds(
            title="1X2. 2-я Партия",
            p1=values["п1"],
            p2=values["п2"],
            status="MARKET_AVAILABLE",
        )
    return TargetMarketOdds(
        title="1X2. 2-я Партия",
        p1=None,
        p2=None,
        status="MARKET_LOCKED" if market_locked or await buttons.count() else "MARKET_LOCKED",
    )


# ForksTableTennisScanner imported these functions into its own module namespace.
# Make every Party-2 helper use the tolerant event pin and the immediate favorite.
base_forks_scanner.select_favorite = select_favorite_immediately
base_forks_scanner.ensure_same_event = ensure_pinned_event
monitoring_module.ensure_same_event = ensure_pinned_event


class SequentialForksTableTennisScanner(ForksTableTennisScanner):
    """One catalog scan -> pin first match -> Party 2 -> first leg -> hedge -> next match.

    The selected event is never abandoned just because Party 2 or its market is not
    ready yet. Missing DOM pieces are treated as WAIT states. A new match is opened
    only after both DEMO legs are locked (or the current Party 2 actually finishes).
    """

    async def _scan_zero_zero_catalog(
        self,
        page: Any,
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
                league_matches = await scan_league_matches(page, league, table_tennis_url)
                collected.extend(league_matches)
                scanned_leagues[index] = replace(
                    league,
                    parsed_games_count=len(league_matches),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(
                    {
                        "league": league.name,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                await self._log("FORKS_LEAGUE_ERROR", errors[-1]["error"])

        unique = deduplicate_match_models(collected)
        candidates = select_zero_zero_in_scan_order(
            unique,
            self._processed_event_ids,
        )
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
        if candidates:
            first = candidates[0]
            await self._log(
                "FORKS_QUEUE_PINNED",
                f"first={first.event_id}; {first.player_1} - {first.player_2}; "
                f"queue={len(candidates)}; DOM order preserved",
            )
        return candidates

    async def _close_locked_arbitrage(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        return settlement

    async def _complete_second_leg(
        self,
        payload: dict[str, Any],
        *,
        odds: float,
    ) -> bool:
        """Lock the hedge at the mathematical zero-fork boundary or above."""
        if payload.get("second_leg") is not None:
            return True
        first = payload.get("first_leg") or {}
        if not first:
            return False

        first_odds = float(first.get("accepted_odd", first["odds"]))
        threshold = minimum_second_odds(first_odds)
        if float(odds) + 1e-9 < threshold:
            return False

        calc = calculate_fork(
            float(first["stake"]),
            first_odds,
            float(odds),
            min_arb_percent=0.0,
        )
        # At exactly the zero boundary calc.profitable is intentionally False in
        # the generic helper, but both rounded outcomes are still break-even.
        if calc.guaranteed_profit < -0.01:
            return False
        if calc.second_stake > self._available_balance:
            payload.update(
                monitoring_status="INSUFFICIENT_BUDGET_SECOND_LEG",
                required_second_stake=calc.second_stake,
                budget_required=calc.total_stake,
            )
            await self._publish_active(payload)
            return False

        side = str(payload.get("second_side"))
        second_leg = {
            "side": side,
            "player": payload[f"player_{1 if side == 'p1' else 2}"],
            "odds": float(odds),
            "accepted_odd": float(odds),
            "stake": calc.second_stake,
            "placed_at": utc_now(),
            "mode": "PAPER",
            "sequence_id": payload.get("sequence_id"),
        }
        self._available_balance = round(self._available_balance - calc.second_stake, 2)
        self._reserved_balance = round(self._reserved_balance + calc.second_stake, 2)
        payload.update(
            second_leg=second_leg,
            required_second_stake=calc.second_stake,
            fork_percent_preview=calc.arbitrage_percent,
            arbitrage_percent_preview=calc.arbitrage_percent,
            fork=calc.to_dict(),
            monitoring_status="SECOND_BET_PLACED",
            market_status="MARKET_AVAILABLE",
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
            f"{side.upper()}; odd={odds:.3f}; threshold={threshold:.3f}; "
            f"stake={calc.second_stake:.2f}",
        )
        await self._close_locked_arbitrage(payload)
        raise ArbitrageLocked

    async def _observe_zero_zero(self, page: Any, match: TableTennisMatch) -> str:
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
            # Navigate only once. On a retry we stay on the already pinned event
            # instead of page.goto() reloading the match and creating visible jumps.
            if not event_url_matches(page.url, match.event_id):
                await page.goto(match.url, wait_until="domcontentloaded", timeout=60_000)
            ensure_pinned_event(page, match.event_id)

            try:
                opened = await read_scoreboard(page)
                payload.update(
                    player_1=opened.team1 or payload.get("player_1"),
                    player_2=opened.team2 or payload.get("player_2"),
                    current_score=opened.score.text(),
                )
            except ScoreReadError:
                pass

            await self._publish_active(payload)
            await self._log(
                "ACTIVE MATCH",
                f"PINNED event={match.event_id}; "
                f"{payload.get('player_1')} vs {payload.get('player_2')}; url={page.url}",
            )

            party_logged = False
            last_wait_message: str | None = None
            while not self._stop_event.is_set():
                ensure_pinned_event(page, match.event_id)

                market = await read_party_two_1x2_odds_direct(page, match.event_id)
                selected_party = await read_selected_party(page)
                if selected_party != self.TARGET_SET and market is None:
                    try:
                        await open_target_party(
                            page,
                            match.event_id,
                            self.TARGET_SET,
                            self._stop_event,
                            timeout=3.0,
                        )
                    except TargetPartyNotAvailable as error:
                        message = str(error)
                        payload.update(
                            monitoring_status="WAITING_SECOND_SET_MARKET",
                            market_status="WAITING_FOR_MARKET",
                            party_wait_reason=message,
                        )
                        await self._publish_active(payload)
                        if message != last_wait_message:
                            await self._log(
                                "WAITING PARTY 2",
                                f"event={match.event_id}; {message}; staying on same match",
                            )
                            last_wait_message = message
                        await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                        continue
                    market = await read_party_two_1x2_odds_direct(page, match.event_id)

                # The exact Party-2 market is a stronger confirmation than a CSS
                # selected marker. This handles SPA re-renders where the selected
                # class is temporarily missing even though Party 2 is already open.
                if market is not None and not party_logged:
                    party_logged = True
                    payload["monitoring_status"] = "SECOND_SET_SELECTED"
                    payload["party_wait_reason"] = None
                    await self._publish_active(payload)
                    await self._log(
                        "SET",
                        f"Party 2 confirmed for pinned event={match.event_id}",
                    )

                winner = await self._read_second_set_winner(page, payload)
                if winner is not None:
                    had_hedge = payload.get("second_leg") is not None
                    await self._settle_series(payload, winner)
                    return "FINISHED" if had_hedge or payload.get("first_leg") is None else "HEDGE_NOT_FOUND"

                if market is None:
                    payload.update(
                        monitoring_status="WAITING_SECOND_SET_MARKET",
                        market_status="WAITING_FOR_MARKET",
                    )
                    await self._publish_active(payload)
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                    continue

                if not market.available:
                    changed = payload.get("market_status") != "MARKET_LOCKED"
                    payload.update(
                        market_status="MARKET_LOCKED",
                        market_available=False,
                        monitoring_status="MARKET_LOCKED",
                        current_odds_p1=None,
                        current_odds_p2=None,
                    )
                    await self._publish_active(payload)
                    if changed:
                        await self._log(
                            "MARKET",
                            "Found exact «1X2. 2-я Партия», but P1/P2 are locked; waiting",
                        )
                    await self._sleep_or_stop(get_table_tennis_odds_poll_interval())
                    continue

                assert market.p1 is not None and market.p2 is not None
                previous_odds = (
                    payload.get("current_odds_p1"),
                    payload.get("current_odds_p2"),
                )
                payload.update(
                    market_status="MARKET_AVAILABLE",
                    market_available=True,
                    market=market.title,
                    current_odds_p1=market.p1,
                    current_odds_p2=market.p2,
                    odds_updated_at=utc_now(),
                )
                if payload.get("initial_odds_p1") is None:
                    payload.update(
                        initial_odds_p1=market.p1,
                        initial_odds_p2=market.p2,
                    )
                    await self._log(
                        "MARKET",
                        f"Found exact parent block: {market.title}",
                    )
                if previous_odds != (market.p1, market.p2):
                    payload.setdefault("odds_history", []).append(
                        {"timestamp": utc_now(), "p1": market.p1, "p2": market.p2}
                    )
                    payload["odds_history"] = payload["odds_history"][-200:]
                    await self._log(
                        "ODDS",
                        f"event={match.event_id}; P1={market.p1:.3f}; P2={market.p2:.3f}",
                    )

                if payload.get("first_leg") is None:
                    side = select_favorite_immediately(market.p1, market.p2)
                    first_odd = market.p1 if side == "p1" else market.p2
                    await self._record_first_leg(payload, side=side, odds=first_odd)
                    # Base method stores the mathematical zero boundary in
                    # minimum_second_odds. First leg is therefore immediate.
                else:
                    side = str(payload.get("second_side"))
                    opposite_odd = market.p1 if side == "p1" else market.p2
                    first = payload["first_leg"]
                    first_odd = float(first.get("accepted_odd", first["odds"]))
                    threshold = minimum_second_odds(first_odd)
                    preview = calculate_fork(
                        float(first["stake"]),
                        first_odd,
                        opposite_odd,
                        min_arb_percent=0.0,
                    )
                    payload.update(
                        monitoring_status="WAITING_FOR_ARB",
                        current_hedge_odd=opposite_odd,
                        minimum_second_odds=threshold,
                        required_second_stake=preview.second_stake,
                        fork_percent_preview=preview.arbitrage_percent,
                        arbitrage_percent_preview=preview.arbitrage_percent,
                        profit_if_first_preview=preview.profit_if_first,
                        profit_if_second_preview=preview.profit_if_second,
                    )
                    if opposite_odd + 1e-9 >= threshold:
                        payload["monitoring_status"] = "ARB_FOUND"
                        await self._publish_active(payload)
                        await self._complete_second_leg(payload, odds=opposite_odd)

                await self._publish_active(payload)
                await self._sleep_or_stop(get_table_tennis_odds_poll_interval())

        except StaleMatchError as error:
            payload.update(
                monitoring_status="ERROR",
                error=str(error),
            )
            await self.state.update_candidate(payload)
            await self.state.set_active_match(payload)
            await self._log(
                "FORKS_PIN_ERROR",
                f"{type(error).__name__}: {error}",
            )
            return "ERROR"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            payload.update(
                monitoring_status="ERROR",
                error=f"{type(error).__name__}: {error}",
            )
            await self.state.update_candidate(payload)
            await self.state.set_active_match(payload)
            await self._log(
                "FORKS_MATCH_ERROR",
                f"event={match.event_id}; {type(error).__name__}: {error}; url={page.url}",
            )
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

                # Exactly one catalog scan per bot start.
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
                        f"PIN event={candidate.event_id}; {candidate.player_1} - "
                        f"{candidate.player_2}; score={candidate.score}",
                    )

                    # Pin this exact candidate. ERROR means retry the same event.
                    # _observe_zero_zero itself avoids page.goto when the URL is
                    # still the same event, so retries no longer look like jumps.
                    while not self._stop_event.is_set():
                        try:
                            outcome = await self._observe_zero_zero(page, candidate)
                        except ArbitrageLocked:
                            outcome = "ARB_LOCKED"

                        if outcome != "ERROR":
                            break

                        await self._log(
                            "FORKS_MATCH_RETRY",
                            f"event={candidate.event_id}; retry same pinned match; url={page.url}",
                        )
                        await self._sleep_or_stop(0.5)

                    if self._stop_event.is_set():
                        break
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


TABLE_TENNIS_SCANNER = SequentialForksTableTennisScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
