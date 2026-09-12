from __future__ import annotations

import asyncio
import re
from typing import Any

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager

from . import forks_scanner as base_forks_scanner
from . import idempotent_sequential_forks_scanner as idempotent_module
from . import monitoring as monitoring_module
from . import sequential_forks_scanner as sequential_module
from .idempotent_sequential_forks_scanner import (
    IdempotentSequentialForksTableTennisScanner,
)
from .monitoring import TargetMarketOdds, TargetPartyNotAvailable
from .state import TABLE_TENNIS_STATE, TableTennisStateStore


_PARTY_ITEM_SELECTOR = ".game-sub-games__list .game-sub-games__item"
_PARTY_CAPTION_SELECTOR = ".ui-caption"
_MARKET_GROUP_SELECTOR = (
    ".game-markets-content__item.game-markets-group, "
    ".game-markets-content__item, .game-markets-group"
)
_MARKET_TITLE_SELECTOR = (
    ".game-markets-group-header-title .ui-caption, "
    ".game-markets-group-header-title"
)
_MARKET_BUTTON_SELECTOR = (
    "button.game-markets-group__market, "
    "button.market, .game-markets-group__market"
)
_MARKET_NAME_SELECTOR = ".ui-market__name"
_MARKET_VALUE_SELECTOR = ".ui-market__value"

# Strategy state is intentionally tiny: one active catalog match at a time and a
# single Party-2 click for that match. We do not compare/validate URLs after the
# match has been opened because 1xBet changes the event URL when a sub-game opens.
_active_event_id: str | None = None
_party_two_clicked_events: set[str] = set()


def _normalize(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _ignore_event_check(*_args: Any, **_kwargs: Any) -> None:
    """Deliberately do nothing: Party 2 is allowed to change the 1xBet event URL."""
    return None


def _current_match_is_open(*_args: Any, **_kwargs: Any) -> bool:
    """The subclass itself performs the one allowed page.goto for each match."""
    return True


async def _node_text(node: Any, selector: str) -> str | None:
    try:
        locator = node.locator(selector).first
        if await locator.count() == 0:
            return None
        text = _normalize(await locator.inner_text())
        return text or None
    except Exception:
        return None


async def _find_party_two_item(page: Any) -> Any | None:
    items = page.locator(_PARTY_ITEM_SELECTOR)
    for index in range(await items.count()):
        item = items.nth(index)
        caption = await _node_text(item, _PARTY_CAPTION_SELECTOR)
        if caption and caption.casefold() in {
            "2-я партия",
            "2-ая партия",
            "2 партия",
        }:
            return item
    return None


async def read_selected_party_simple(_page: Any) -> int | None:
    """After our one Party-2 click, treat the strategy as being in Party 2.

    No selected-class or URL verification is performed. This is intentional:
    those checks caused the Main game <-> Party 2 reload loop.
    """
    if _active_event_id and _active_event_id in _party_two_clicked_events:
        return 2
    return None


async def open_party_two_once(
    page: Any,
    event_id: str,
    target_set: int,
    stop_event: asyncio.Event,
    *,
    timeout: float = 15.0,
) -> None:
    """Find `2-я Партия`, click it exactly once, then let the page render.

    There is no URL check, no selected-CSS check and no second click. If the tab
    has not appeared yet, stay on the same page and wait for it.
    """
    if target_set != 2:
        raise TargetPartyNotAvailable(
            f"Эта стратегия работает только со 2-й партией, получено: {target_set}."
        )

    key = str(event_id)
    if key in _party_two_clicked_events:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout), 15.0)
    while loop.time() < deadline and not stop_event.is_set():
        item = await _find_party_two_item(page)
        if item is None:
            await asyncio.sleep(0.25)
            continue

        try:
            await item.scroll_into_view_if_needed()
            await item.click(timeout=3_000)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The DOM may be re-rendering. Do not navigate anywhere; just keep
            # waiting for the same Party-2 tab and try the first click later.
            await asyncio.sleep(0.5)
            continue

        _party_two_clicked_events.add(key)

        # Important: after the single click, do nothing to navigation. Give 1xBet
        # time to redraw the Party-2 market. The normal odds polling continues
        # after this grace period.
        await asyncio.sleep(3.0)
        return

    if stop_event.is_set():
        raise asyncio.CancelledError
    raise TargetPartyNotAvailable(
        "Вкладка «2-я Партия» пока не появилась; остаёмся на текущем матче."
    )


def _market_title_kind(value: str | None) -> int:
    """0=not target, 1=Party-2 fallback title, 2=exact `1X2` inside sub-game."""
    title = _normalize(value).casefold().replace("х", "x")
    if title == "1x2":
        return 2
    if not title.startswith("1x2"):
        return 0
    compact = title.replace("ё", "е")
    if re.search(r"\b2\s*[-–—]?\s*(?:я|ая)?\s*партия\b", compact):
        return 1
    return 0


async def read_party_two_1x2_simple(
    page: Any,
    event_id: str,
) -> TargetMarketOdds | None:
    """Read only P1/P2 from the Party-2 1X2 market, without URL validation."""
    key = str(event_id)
    if key not in _party_two_clicked_events:
        return None

    groups = page.locator(_MARKET_GROUP_SELECTOR)
    exact_group = None
    fallback_group = None

    for index in range(await groups.count()):
        group = groups.nth(index)
        title = await _node_text(group, _MARKET_TITLE_SELECTOR)
        kind = _market_title_kind(title)
        if kind == 2:
            exact_group = group
            break
        if kind == 1 and fallback_group is None:
            fallback_group = group

    matched_group = exact_group if exact_group is not None else fallback_group
    if matched_group is None:
        return None

    buttons = matched_group.locator(_MARKET_BUTTON_SELECTOR)
    if await buttons.count() == 0:
        # Opening a market accordion is not navigation and does not change match
        # or party. Do it once if the site's body is collapsed.
        header = matched_group.locator(".game-markets-group__header").first
        try:
            if await header.count():
                await header.click(timeout=2_000)
                await asyncio.sleep(0.35)
                buttons = matched_group.locator(_MARKET_BUTTON_SELECTOR)
        except Exception:
            pass

    values: dict[str, float] = {}
    saw_target_button = False

    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = _normalize((await _node_text(button, _MARKET_NAME_SELECTOR)) or "")
        key_name = name.casefold().replace(" ", "")
        if key_name in {"п1", "p1", "1"}:
            side = "p1"
        elif key_name in {"п2", "p2", "2"}:
            side = "p2"
        else:
            continue

        saw_target_button = True
        value = await _node_text(button, _MARKET_VALUE_SELECTOR)
        if not value:
            continue

        raw = value.replace(",", ".").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw) is None:
            continue
        parsed = float(raw)
        if parsed <= 1.0:
            continue
        values[side] = parsed

    if "p1" in values and "p2" in values:
        return TargetMarketOdds(
            title="1X2 · 2-я Партия",
            p1=values["p1"],
            p2=values["p2"],
            status="MARKET_AVAILABLE",
        )

    if saw_target_button:
        return TargetMarketOdds(
            title="1X2 · 2-я Партия",
            p1=None,
            p2=None,
            status="MARKET_LOCKED",
        )
    return None


# Remove the URL/event and selected-state checks from the active sequential flow.
# The only navigation is performed by SimplePartyTwoForksScanner below.
sequential_module.event_url_matches = _current_match_is_open
sequential_module.ensure_pinned_event = _ignore_event_check
sequential_module.read_selected_party = read_selected_party_simple
sequential_module.open_target_party = open_party_two_once
sequential_module.read_party_two_1x2_odds_direct = read_party_two_1x2_simple

base_forks_scanner.ensure_same_event = _ignore_event_check
monitoring_module.ensure_same_event = _ignore_event_check
idempotent_module.ensure_pinned_event = _ignore_event_check


class SimplePartyTwoForksScanner(IdempotentSequentialForksTableTennisScanner):
    """Minimal requested flow: open match once -> Party 2 once -> monitor odds."""

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager=browser_manager, state=state)
        self._opened_event_id: str | None = None

    async def configure(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        global _active_event_id
        _active_event_id = None
        _party_two_clicked_events.clear()
        self._opened_event_id = None
        return await super().configure(budget=budget, initial_stake=initial_stake)

    async def _observe_zero_zero(self, page: Any, match: Any) -> str:
        global _active_event_id

        event_id = str(match.event_id or "")
        if not event_id or not match.url:
            return "INVALID_MATCH"

        # Exactly one navigation for a queue item. Nothing inside the Party-2
        # observation loop is allowed to navigate back to this root URL.
        if self._opened_event_id != event_id:
            await page.goto(match.url, wait_until="domcontentloaded", timeout=60_000)
            self._opened_event_id = event_id
            _active_event_id = event_id
            _party_two_clicked_events.discard(event_id)

            # Let the initial match page render before looking for the sub-game tabs.
            await asyncio.sleep(2.0)
            await self._log(
                "MATCH OPENED",
                f"event={event_id}; opened once; waiting for «2-я Партия»",
            )

        return await super()._observe_zero_zero(page, match)


TABLE_TENNIS_SCANNER = SimplePartyTwoForksScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
