import asyncio
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from playwright.async_api import Locator, Page

from .models import TableTennisMatch
from .selectors import (
    CAPTION_SELECTOR,
    MARKET_CONTENT_ITEM_SELECTOR,
    MARKET_GROUP_HEADER_SELECTOR,
    MARKET_GROUP_LIST_SELECTOR,
    MARKET_GROUP_SELECTION_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_VALUE_SELECTOR,
    PERIOD_SELECTOR,
    SELECTED_SUB_GAME_SELECTOR,
    SUB_GAME_ITEM_SELECTOR,
    SUB_GAMES_LIST_SELECTOR,
)


PARTY_PATTERN = re.compile(
    r"(?<!\d)(\d+)\s*(?:[-–—]\s*(?:я|й|ая))?\s*(?:партия|партии|партию|сет)\b",
    re.IGNORECASE,
)
TARGET_MARKET_BUTTON_SELECTOR = (
    f"{MARKET_GROUP_LIST_SELECTOR} .game-markets-group__market, "
    f"{MARKET_GROUP_LIST_SELECTOR} .market"
)


class StaleMatchError(RuntimeError):
    pass


class TargetPartyNotAvailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetMarketOdds:
    title: str
    p1: float | None
    p2: float | None
    status: str

    @property
    def available(self) -> bool:
        return (
            self.status == "MARKET_AVAILABLE"
            and self.p1 is not None
            and self.p2 is not None
        )


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def parse_party_number(value: str | None) -> int | None:
    """Read a party number only when DOM text explicitly names a party/set."""
    match = PARTY_PATTERN.search(normalize_text(value).casefold())
    return int(match.group(1)) if match else None


def is_candidate_set(current_set: int | None) -> bool:
    return current_set in (1, 2)


def target_set_for(current_set: int | None) -> int | None:
    return current_set + 1 if is_candidate_set(current_set) else None


def mark_candidate(match: TableTennisMatch) -> TableTennisMatch:
    return replace(match, is_candidate=is_candidate_set(match.current_set))


def select_candidate_matches(
    matches: list[TableTennisMatch],
) -> list[TableTennisMatch]:
    """Keep scanner order stable; no random or odds-based selection."""
    return [match for match in matches if is_candidate_set(match.current_set)]


def candidate_payload(match: TableTennisMatch) -> dict[str, Any]:
    result = match.to_dict()
    result.update(
        target_set=target_set_for(match.current_set),
        match_score=match.score,
        set_score=match.sets_score,
        initial_odds_p1=None,
        initial_odds_p2=None,
        current_odds_p1=None,
        current_odds_p2=None,
        initial_odds_at=None,
        odds_updated_at=None,
        market_status="WAITING_FOR_MARKET",
        monitoring_status="CANDIDATE",
        odds_history=[],
        outcomes=map_outcomes_to_players(
            match.player_1,
            match.player_2,
            None,
            None,
        ),
    )
    return result


def target_market_title(target_set: int) -> str:
    return f"1X2. {target_set}-я Партия"


def map_outcomes_to_players(
    player_1: str,
    player_2: str,
    p1: float | None,
    p2: float | None,
) -> dict[str, dict[str, Any]]:
    return {
        "p1": {"player": player_1, "odds": p1},
        "p2": {"player": player_2, "odds": p2},
    }


async def _first_party_number(locator: Locator) -> int | None:
    try:
        for index in range(await locator.count()):
            value = parse_party_number(await locator.nth(index).inner_text())
            if value is not None:
                return value
    except Exception:
        return None
    return None


async def read_selected_party(page: Page) -> int | None:
    selected = page.locator(
        f"{SUB_GAMES_LIST_SELECTOR} {SELECTED_SUB_GAME_SELECTOR}"
    )
    return await _first_party_number(selected)


async def read_current_party(
    page: Page,
    *,
    allow_selected_fallback: bool = False,
) -> int | None:
    """Read the factual live-period label, never score-derived guesses."""
    current = await _first_party_number(page.locator(PERIOD_SELECTOR))
    if current is not None or not allow_selected_fallback:
        return current
    return await read_selected_party(page)


def page_event_id(page: Page) -> str | None:
    from .scanner import parse_event_id

    return parse_event_id(page.url)


def ensure_same_event(page: Page, expected_event_id: str) -> None:
    actual_event_id = page_event_id(page)
    if actual_event_id != expected_event_id:
        raise StaleMatchError(
            f"Ожидался event_id={expected_event_id}, открыт event_id={actual_event_id or 'UNKNOWN'}."
        )


async def find_target_party_item(page: Page, target_set: int) -> Locator | None:
    """Find the concrete sub-game tab by its visible caption, not by Vue data-v attributes."""
    items = page.locator(f"{SUB_GAMES_LIST_SELECTOR} {SUB_GAME_ITEM_SELECTOR}")
    for index in range(await items.count()):
        item = items.nth(index)
        caption = item.locator(CAPTION_SELECTOR).first
        if await caption.count() == 0:
            continue
        try:
            text = await caption.inner_text()
        except Exception:
            continue
        if parse_party_number(text) == target_set:
            return item
    return None


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass
    if stop_event.is_set():
        raise asyncio.CancelledError


async def _wait_until_party_selected(
    page: Page,
    event_id: str,
    target_set: int,
    stop_event: asyncio.Event,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ensure_same_event(page, event_id)
        if await read_selected_party(page) == target_set:
            return True
        await _wait_or_stop(stop_event, 0.1)
    return False


async def open_target_party(
    page: Page,
    event_id: str,
    target_set: int,
    stop_event: asyncio.Event,
    *,
    timeout: float = 15.0,
) -> None:
    """Open a party tab and verify that the site really changed the selected sub-game.

    The table-tennis page initially opens on «Основная игра». The sub-games list is
    rendered asynchronously, so a single immediate lookup/click is unreliable.
    """
    ensure_same_event(page, event_id)

    list_locator = page.locator(SUB_GAMES_LIST_SELECTOR).first
    try:
        await list_locator.wait_for(state="attached", timeout=min(timeout * 1000, 15_000))
    except Exception as error:
        raise TargetPartyNotAvailable("Список партий не появился на странице матча.") from error

    deadline = time.monotonic() + timeout
    item: Locator | None = None
    while time.monotonic() < deadline and not stop_event.is_set():
        ensure_same_event(page, event_id)
        item = await find_target_party_item(page, target_set)
        if item is not None:
            break
        await _wait_or_stop(stop_event, 0.1)

    if item is None:
        raise TargetPartyNotAvailable(
            f"Вкладка «{target_set}-я Партия» отсутствует."
        )

    if await read_selected_party(page) == target_set:
        return

    # Click the visible caption first. The site's handler is attached to the tab
    # and the child click bubbles to it. If the normal Playwright click is
    # intercepted during a re-render, retry on the LI and then dispatch a click.
    click_targets = [item.locator(CAPTION_SELECTOR).first, item]
    last_error: Exception | None = None
    for target in click_targets:
        ensure_same_event(page, event_id)
        try:
            await target.scroll_into_view_if_needed()
            await target.click(timeout=3_000)
            if await _wait_until_party_selected(
                page,
                event_id,
                target_set,
                stop_event,
                timeout=2.0,
            ):
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = error

    # Last fallback for the same visible tab. This does not use any data-v-* selector.
    try:
        ensure_same_event(page, event_id)
        item = await find_target_party_item(page, target_set)
        if item is not None:
            await item.dispatch_event("click")
            if await _wait_until_party_selected(
                page,
                event_id,
                target_set,
                stop_event,
                timeout=max(1.0, min(3.0, timeout)),
            ):
                return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        last_error = error

    message = f"Вкладка «{target_set}-я Партия» найдена, но сайт не переключил её в выбранное состояние."
    if last_error is not None:
        message += f" Последняя ошибка: {type(last_error).__name__}: {last_error}"
    raise TargetPartyNotAvailable(message)


async def find_target_market_group(page: Page, target_set: int) -> Locator | None:
    """Find the 1X2 market inside the already selected party.

    On the current site, after selecting «1-я партия» the accordion is titled simply
    «1X2». Older layouts used «1X2. 1-я Партия», so both forms are supported.
    """
    expected_party_title = normalize_text(target_market_title(target_set)).casefold().replace("х", "x")
    groups = page.locator(
        f"{MARKET_CONTENT_ITEM_SELECTOR} {MARKET_GROUP_SELECTOR}, "
        f"{MARKET_GROUP_SELECTOR}"
    )
    title_selector = f"{MARKET_GROUP_HEADER_SELECTOR} {MARKET_GROUP_TITLE_SELECTOR}"
    for index in range(await groups.count()):
        group = groups.nth(index)
        title = group.locator(title_selector).first
        if await title.count() == 0:
            continue
        actual = normalize_text(await title.inner_text()).casefold().replace("х", "x")
        if actual == "1x2" or actual == expected_party_title:
            return group
    return None


async def read_target_market_odds(
    page: Page,
    event_id: str,
    target_set: int,
) -> TargetMarketOdds | None:
    ensure_same_event(page, event_id)
    group = await find_target_market_group(page, target_set)
    if group is None:
        return None

    values: dict[str, float] = {}
    locked = False
    buttons = group.locator(TARGET_MARKET_BUTTON_SELECTOR)
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = button.locator(MARKET_NAME_SELECTOR).first
        if await name.count() == 0:
            continue
        outcome = normalize_text(await name.inner_text()).casefold()
        if outcome not in {"п1", "п2"}:
            continue
        classes = (await button.get_attribute("class") or "").casefold()
        try:
            disabled = await button.is_disabled()
        except Exception:
            disabled = False
        if disabled or "locked" in classes or "disabled" in classes:
            locked = True
            continue
        value = button.locator(MARKET_VALUE_SELECTOR).first
        if await value.count() == 0:
            locked = True
            continue
        raw_value = normalize_text(await value.inner_text()).replace(",", ".")
        if re.fullmatch(r"\d+(?:\.\d+)?", raw_value) is None:
            locked = True
            continue
        parsed = float(raw_value)
        if parsed <= 0:
            locked = True
            continue
        values[outcome] = parsed

    ensure_same_event(page, event_id)
    if "п1" not in values or "п2" not in values:
        return TargetMarketOdds(
            title=target_market_title(target_set),
            p1=None,
            p2=None,
            status="MARKET_LOCKED",
        )
    return TargetMarketOdds(
        title=target_market_title(target_set),
        p1=values["п1"],
        p2=values["п2"],
        status="MARKET_AVAILABLE",
    )
