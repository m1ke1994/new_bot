from __future__ import annotations

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
PERIOD_CONTROL_SELECTOR = "button, a, [role='tab']"
PERIOD_SIGNAL_SELECTORS = (
    ".scoreboard-live__status",
    '[role="tab"][aria-selected="true"]',
    'button[aria-pressed="true"]',
    ".game-toolbar-filter-switch--active",
    ".game-toolbar-filter-switch--is-active",
    ".game-subgames__item--active",
    ".game-tabs__item--active",
)


class FirstHalfNotReady(RuntimeError):
    pass


def _clean(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


async def open_first_half(page: Page, logger: Logger | None = None) -> str:
    """Open the exact first-half sub-game instead of relying on element order."""
    controls = page.locator(PERIOD_CONTROL_SELECTOR)
    for index in range(await controls.count()):
        control = controls.nth(index)
        try:
            if not await control.is_visible():
                continue
            if _clean(await control.inner_text()) != _clean(FIRST_HALF_LABEL):
                continue
            await control.click(timeout=5_000)
            if logger is not None:
                await logger("FIRST_HALF_OPENED", "[FIRST_HALF_DRAW] Opening 1-й тайм")
            return FIRST_HALF_LABEL
        except Exception:
            continue
    raise FirstHalfNotReady("Кнопка sub-game «1-й тайм» пока не найдена.")


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
