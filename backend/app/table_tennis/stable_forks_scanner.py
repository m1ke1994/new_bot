from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from backend.app.browser.manager import BROWSER_MANAGER, BrowserManager

from . import sequential_forks_scanner as sequential_module
from .idempotent_sequential_forks_scanner import (
    IdempotentSequentialForksTableTennisScanner,
)
from .monitoring import TargetMarketOdds, TargetPartyNotAvailable
from .scanner import ScanAlreadyRunning, TableTennisScanError
from .sequential_forks_scanner import ArbitrageLocked, ensure_pinned_event
from .state import TABLE_TENNIS_STATE, TableTennisStateStore


_PARTY_ITEM_SELECTOR = ".game-sub-games__list .game-sub-games__item"
_PARTY_CAPTION_SELECTOR = ".ui-caption"
_SELECTED_PARTY_SELECTOR = (
    ".game-sub-games__list .game-sub-games__item.game-sub-games__item--is-selected, "
    ".game-sub-games__list .game-sub-games__item.game-sub-games__item--active, "
    ".game-sub-games__list .game-sub-games__item[aria-selected=\"true\"]"
)
_PARTY_RE = re.compile(r"^\s*(\d+)\s*[-–—]?\s*(?:я|й|ая)?\s*партия\s*$", re.IGNORECASE)
_PARTY_TWO_RE = re.compile(r"\b2\s*[-–—]?\s*(?:я|й|ая)?\s+партия\b", re.IGNORECASE)

# Prevent a fast click/reload/click loop on the same event. One click is sent and
# then the site is given time to finish its SPA/full-page refresh before another
# attempt is allowed.
_PARTY_CLICK_COOLDOWN_SECONDS = 8.0
_PARTY_SELECTION_STABLE_SECONDS = 0.8
_party_click_at: dict[str, float] = {}


def _normalize(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _party_number(value: str | None) -> int | None:
    match = _PARTY_RE.fullmatch(_normalize(value))
    return int(match.group(1)) if match else None


def _normalize_market_title(value: str | None) -> str:
    return _normalize(value).casefold().replace("х", "x")


def is_party_two_1x2_title(value: str | None) -> bool:
    """Accept the two real layouts used by 1xBet after Party 2 is selected.

    On the main game the site often renders `1X2. 2-я Партия`. After switching
    into the Party-2 sub-game many leagues render the same market simply as
    `1X2`. We only call this parser after Party 2 is confirmed selected, so the
    short title is safe and must be supported.
    """
    title = _normalize_market_title(value)
    if title == "1x2":
        return True
    if not title.startswith("1x2"):
        return False
    if "партия" not in title:
        return True
    return _PARTY_TWO_RE.search(title) is not None


async def _node_text(node: Any, selector: str) -> str | None:
    try:
        locator = node.locator(selector).first
        if await locator.count() == 0:
            return None
        value = _normalize(await locator.inner_text())
        return value or None
    except Exception:
        return None


async def _caption_text(item: Any) -> str | None:
    return await _node_text(item, _PARTY_CAPTION_SELECTOR)


async def find_party_item(page: Any, target_set: int = 2) -> Any | None:
    items = page.locator(_PARTY_ITEM_SELECTOR)
    for index in range(await items.count()):
        item = items.nth(index)
        if _party_number(await _caption_text(item)) == target_set:
            return item
    return None


async def read_selected_party_stable(page: Any) -> int | None:
    selected = page.locator(_SELECTED_PARTY_SELECTOR)
    for index in range(await selected.count()):
        number = _party_number(await _caption_text(selected.nth(index)))
        if number is not None:
            return number
    return None


async def open_party_two_stable(
    page: Any,
    event_id: str,
    target_set: int,
    stop_event: asyncio.Event,
    *,
    timeout: float = 15.0,
) -> None:
    """Click Party 2 once, then wait for the refreshed DOM to settle.

    The previous implementation clicked the LI, then the caption, then dispatched
    another click while the site was still refreshing. On 1xBet that can create a
    visible Main game -> Party 2 -> reload -> Main game loop. This implementation
    sends one click per cooldown window and waits for Party 2 to remain selected.
    """
    if target_set != 2:
        return await sequential_module._ORIGINAL_OPEN_TARGET_PARTY(  # type: ignore[attr-defined]
            page,
            event_id,
            target_set,
            stop_event,
            timeout=timeout,
        )

    wait_seconds = max(float(timeout), 6.0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    stable_since: float | None = None

    while loop.time() < deadline and not stop_event.is_set():
        ensure_pinned_event(page, event_id)
        if await read_selected_party_stable(page) == 2:
            now = loop.time()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= _PARTY_SELECTION_STABLE_SECONDS:
                return
        else:
            stable_since = None

        last_click = _party_click_at.get(str(event_id), 0.0)
        now = time.monotonic()
        if now - last_click >= _PARTY_CLICK_COOLDOWN_SECONDS:
            item = await find_party_item(page, 2)
            if item is not None:
                try:
                    await item.scroll_into_view_if_needed()
                    await item.click(timeout=2_000)
                    _party_click_at[str(event_id)] = time.monotonic()
                    # Do not click anything else while the SPA/full-page refresh
                    # is in progress. Reacquire all locators on the next loop.
                    stable_since = None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Keep waiting on the same event. The caller/browser manager
                    # handles a genuinely closed page/context separately.
                    pass

        await asyncio.sleep(0.20)

    if stop_event.is_set():
        raise asyncio.CancelledError
    raise TargetPartyNotAvailable(
        "Вкладка «2-я Партия» ещё не закрепилась после обновления страницы; "
        "остаёмся на текущем матче и не читаем коэффициенты основной игры."
    )


async def read_party_two_1x2_stable(
    page: Any,
    event_id: str,
) -> TargetMarketOdds | None:
    """Read P1/P2 only from the currently selected Party-2 sub-game."""
    ensure_pinned_event(page, event_id)
    if await read_selected_party_stable(page) != 2:
        return None

    groups = page.locator(
        ".game-markets-content__item.game-markets-group, "
        ".game-markets-content__item, .game-markets-group"
    )
    exact_group = None
    fallback_group = None

    for index in range(await groups.count()):
        group = groups.nth(index)
        title = await _node_text(
            group,
            ".game-markets-group-header-title .ui-caption, "
            ".game-markets-group-header-title",
        )
        if not is_party_two_1x2_title(title):
            continue
        normalized = _normalize_market_title(title)
        if normalized == "1x2":
            exact_group = group
            break
        fallback_group = fallback_group or group

    matched_group = exact_group or fallback_group
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
                await asyncio.sleep(0.20)
                buttons = matched_group.locator(
                    "button.game-markets-group__market, button.market, .game-markets-group__market"
                )
        except Exception:
            pass

    values: dict[str, float] = {}
    market_locked = False
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = _normalize((await _node_text(button, ".ui-market__name")) or "").casefold()
        value = await _node_text(button, ".ui-market__value")
        key = name.replace(" ", "")
        side: str | None = None
        if key in {"п1", "p1", "1"}:
            side = "p1"
        elif key in {"п2", "p2", "2"}:
            side = "p2"
        if side is None:
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

        raw = value.replace(",", ".").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw) is None:
            market_locked = True
            continue
        parsed = float(raw)
        if parsed <= 1.0:
            market_locked = True
            continue
        values[side] = parsed

    ensure_pinned_event(page, event_id)
    if "p1" in values and "p2" in values:
        return TargetMarketOdds(
            title="1X2 · 2-я Партия",
            p1=values["p1"],
            p2=values["p2"],
            status="MARKET_AVAILABLE",
        )
    return TargetMarketOdds(
        title="1X2 · 2-я Партия",
        p1=None,
        p2=None,
        status="MARKET_LOCKED" if market_locked or await buttons.count() else "MARKET_LOCKED",
    )


# Override the helpers used by SequentialForksTableTennisScanner. Importing the
# idempotent scanner above applies its legacy monkey patches first; these are the
# final stable versions used by the route singleton below.
sequential_module.read_selected_party = read_selected_party_stable
sequential_module.open_target_party = open_party_two_stable
sequential_module.read_party_two_1x2_odds_direct = read_party_two_1x2_stable


class StableTableTennisForksScanner(IdempotentSequentialForksTableTennisScanner):
    """Stable sequential paper strategy with browser-page recovery."""

    def __init__(
        self,
        browser_manager: BrowserManager = BROWSER_MANAGER,
        state: TableTennisStateStore = TABLE_TENNIS_STATE,
    ) -> None:
        super().__init__(browser_manager=browser_manager, state=state)

    async def configure(self, *, budget: float, initial_stake: float) -> dict[str, Any]:
        _party_click_at.clear()
        return await super().configure(budget=budget, initial_stake=initial_stake)

    async def _run_strategy(self) -> None:
        if self._scan_lock.locked():
            raise ScanAlreadyRunning("Сканирование настольного тенниса уже выполняется.")
        if self._budget <= 0 or self._initial_stake <= 0:
            raise TableTennisScanError("Перед запуском задайте бюджет и первоначальную ставку.")

        async with self._scan_lock:
            try:
                page, table_tennis_url = await self._authorize_page()
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

                    outcome = "ERROR"
                    while not self._stop_event.is_set():
                        # Critical fix for TargetClosedError: never reuse the Page
                        # object from a dead context. ensure_page() returns the same
                        # live page when healthy and transparently recreates it when
                        # Chromium/page/context was closed.
                        try:
                            page = await self.browser_manager.ensure_page()
                            await self.state.update(
                                browser=await self.browser_manager.snapshot(),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            await self._log(
                                "FORKS_PAGE_RECOVERY_ERROR",
                                f"event={candidate.event_id}; {type(error).__name__}: {error}",
                            )
                            await self._sleep_or_stop(1.0)
                            continue

                        try:
                            outcome = await self._observe_zero_zero(page, candidate)
                        except ArbitrageLocked:
                            outcome = "ARB_LOCKED"

                        if outcome != "ERROR":
                            break

                        # _observe_zero_zero may have caught TargetClosedError and
                        # returned ERROR. Discard the local Page reference and ask
                        # BrowserManager for a live one before trying the SAME event.
                        try:
                            page = await self.browser_manager.ensure_page()
                            current_url = page.url
                        except Exception as recovery_error:
                            current_url = "RECOVERY_FAILED"
                            await self._log(
                                "FORKS_PAGE_RECOVERY_ERROR",
                                f"event={candidate.event_id}; "
                                f"{type(recovery_error).__name__}: {recovery_error}",
                            )

                        await self._log(
                            "FORKS_MATCH_RETRY",
                            f"event={candidate.event_id}; retry same pinned match; "
                            f"live_url={current_url}",
                        )
                        await self._sleep_or_stop(0.75)

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


TABLE_TENNIS_SCANNER = StableTableTennisForksScanner(
    browser_manager=BROWSER_MANAGER,
    state=TABLE_TENNIS_STATE,
)
