from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page

from backend.app.demo.models import ScoreboardSnapshot
from backend.app.demo.strategies.first_half_draw import is_first_half_finished

from .market import (
    FIRST_HALF_1X2_TEXT,
    FIRST_HALF_DRAW_SELECTION_TEXT,
    MARKET_BUTTON_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
)


Logger = Callable[[str, str], Awaitable[Any]]
FIRST_HALF_LABEL = "1-й тайм"
SUBGAME_LIST_SELECTOR = ".game-sub-games__list"
SUBGAME_ITEM_SELECTOR = ".game-sub-games__item"
SUBGAME_CAPTION_SELECTOR = ".ui-caption"
SUBGAME_SELECTED_CLASS = "game-sub-games__item--is-selected"
FIRST_HALF_OPEN_RETRIES = 3
FIRST_HALF_VERIFY_POLLS = 20
FIRST_HALF_VERIFY_DELAY = 0.15
PERIOD_SIGNAL_SELECTORS = (
    ".scoreboard-live__status",
    '[role="tab"][aria-selected="true"]',
    'button[aria-pressed="true"]',
    ".game-sub-games__item--is-selected",
    ".game-toolbar-filter-switch--active",
    ".game-toolbar-filter-switch--is-active",
    ".game-subgames__item--active",
    ".game-tabs__item--active",
)


class FirstHalfNotReady(RuntimeError):
    pass


def _clean(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


async def _subgame_text(item: Any) -> str:
    """Read the visible label of one sub-game item without relying on its index."""
    try:
        caption = item.locator(SUBGAME_CAPTION_SELECTOR).first
        if await caption.count():
            return " ".join((await caption.inner_text()).split())
    except Exception:
        pass
    try:
        return " ".join((await item.inner_text()).split())
    except Exception:
        return ""


async def selected_subgame_text(page: Page) -> str | None:
    """Return the label of the sub-game that the site really marks as selected."""
    items = page.locator(f"{SUBGAME_LIST_SELECTOR} {SUBGAME_ITEM_SELECTOR}")
    try:
        count = await items.count()
    except Exception:
        return None
    for index in range(count):
        item = items.nth(index)
        try:
            classes = await item.get_attribute("class") or ""
        except Exception:
            continue
        if SUBGAME_SELECTED_CLASS not in classes.split():
            continue
        text = await _subgame_text(item)
        return text or None
    return None


async def open_first_half(page: Page, logger: Logger | None = None) -> str:
    """Click the exact «1-й тайм» li and verify that the site selected it."""
    root = page.locator(SUBGAME_LIST_SELECTOR).first
    try:
        await root.wait_for(state="visible", timeout=8_000)
    except Exception as error:
        raise FirstHalfNotReady(
            "Контейнер sub-game .game-sub-games__list пока не найден."
        ) from error

    last_selected = await selected_subgame_text(page)
    for attempt in range(1, FIRST_HALF_OPEN_RETRIES + 1):
        items = page.locator(f"{SUBGAME_LIST_SELECTOR} {SUBGAME_ITEM_SELECTOR}")
        try:
            count = await items.count()
        except Exception as error:
            raise FirstHalfNotReady("Не удалось прочитать список sub-game.") from error

        target = None
        for index in range(count):
            item = items.nth(index)
            try:
                if not await item.is_visible():
                    continue
                text = await _subgame_text(item)
                classes = await item.get_attribute("class") or ""
            except Exception:
                continue

            if _clean(text) != _clean(FIRST_HALF_LABEL):
                continue

            if SUBGAME_SELECTED_CLASS in classes.split():
                if logger is not None:
                    await logger(
                        "FIRST_HALF_SELECTED",
                        "[FIRST_HALF_DRAW] Selected subgame: 1-й тайм",
                    )
                return FIRST_HALF_LABEL
            target = item
            break

        if target is None:
            raise FirstHalfNotReady(
                "В .game-sub-games__list не найден элемент .game-sub-games__item с текстом «1-й тайм»."
            )

        if logger is not None:
            await logger(
                "FIRST_HALF_CURRENT_SUBGAME",
                f"[FIRST_HALF_DRAW] Current selected subgame: {last_selected or 'не определён'}",
            )
            await logger(
                "FIRST_HALF_CLICK",
                f"[FIRST_HALF_DRAW] Clicking: {FIRST_HALF_LABEL} (attempt {attempt}/{FIRST_HALF_OPEN_RETRIES})",
            )

        try:
            await target.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass
        try:
            await target.click(timeout=5_000)
        except Exception as error:
            if attempt >= FIRST_HALF_OPEN_RETRIES:
                raise FirstHalfNotReady(
                    "Не удалось кликнуть sub-game «1-й тайм»."
                ) from error
            await asyncio.sleep(FIRST_HALF_VERIFY_DELAY)
            continue

        # Vue re-renders this block after the click. Do not trust the old locator:
        # repeatedly read fresh locators until the real selected class moves to 1-й тайм.
        for _ in range(FIRST_HALF_VERIFY_POLLS):
            selected = await selected_subgame_text(page)
            last_selected = selected
            if selected is not None and _clean(selected) == _clean(FIRST_HALF_LABEL):
                if logger is not None:
                    await logger(
                        "FIRST_HALF_SELECTED",
                        "[FIRST_HALF_DRAW] Selected after click: 1-й тайм",
                    )
                return FIRST_HALF_LABEL
            await asyncio.sleep(FIRST_HALF_VERIFY_DELAY)

        if logger is not None:
            await logger(
                "FIRST_HALF_SELECTION_RETRY",
                "[FIRST_HALF_DRAW] 1-й тайм did not become selected; retrying",
            )

    raise FirstHalfNotReady(
        "После клика «1-й тайм» не получил класс "
        f"{SUBGAME_SELECTED_CLASS}; выбранная вкладка: {last_selected or 'не определена'}."
    )


async def first_half_end_signal(
    page: Page, snapshot: ScoreboardSnapshot
) -> tuple[bool, str]:
    """Read explicit period/terminal signals from scoreboard and active controls."""
    signals = [snapshot.period, snapshot.timer]
    evidence = [value for value in signals if value]
    for selector in PERIOD_SIGNAL_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            node = locator.nth(index)
            try:
                if not await node.is_visible():
                    continue
                text = " ".join((await node.inner_text()).split())
            except Exception:
                continue
            if text and not re.fullmatch(r"\d{1,3}:\d{2}", text):
                signals.append(text)
                evidence.append(text)
    finished = is_first_half_finished(snapshot, *signals)
    return finished, " | ".join(dict.fromkeys(evidence))


async def first_half_draw_market_present(page: Page) -> bool:
    """Check whether the exact first-half draw outcome still exists in LIVE DOM."""
    groups = page.locator(MARKET_GROUP_SELECTOR)
    try:
        count = await groups.count()
    except Exception:
        return False
    for group_index in range(count):
        group = groups.nth(group_index)
        title = group.locator(MARKET_GROUP_TITLE_SELECTOR).first
        try:
            if await title.count() == 0:
                continue
            if _clean(await title.inner_text()) != _clean(FIRST_HALF_1X2_TEXT):
                continue
            buttons = group.locator(MARKET_BUTTON_SELECTOR)
            for button_index in range(await buttons.count()):
                name = buttons.nth(button_index).locator(MARKET_NAME_SELECTOR).first
                if await name.count() and _clean(await name.inner_text()) == _clean(
                    FIRST_HALF_DRAW_SELECTION_TEXT
                ):
                    return True
            return False
        except Exception:
            continue
    return False
