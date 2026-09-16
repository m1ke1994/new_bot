import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page, Response

from backend.app.demo.models import FirstHalfDrawMarket, NextGoalOdds
from xbet_config import SELECTORS, TEXTS

from .canvas_vision import (
    CANVAS_SELECTOR,
    CanvasVisionError,
    analyze_market_canvas,
    read_market_odds_from_canvas,
)


Logger = Callable[[str, str], Awaitable[Any]]

GOALS_TEXT = TEXTS.goals or "Голы"
GOALS_FILTER_SELECTOR = SELECTORS.goals_filter or ".game-toolbar-filter-switch"
NEXT_GOAL_SEARCH_SELECTOR = SELECTORS.market_search or "input.game-search__input"
MARKET_SEARCH_BUTTON_SELECTOR = (
    os.getenv("SELECTOR_MARKET_SEARCH_BUTTON", "").strip()
    or "button.ui-search-default__button"
)
MARKET_GROUP_SELECTOR = SELECTORS.market_group or ".game-markets-group"
MARKET_GROUP_TITLE_SELECTOR = (
    SELECTORS.market_group_title or ".game-markets-group-header-title"
)
MARKET_BUTTON_SELECTOR = SELECTORS.market_button or "button.game-markets-group__market"
MARKET_NAME_SELECTOR = SELECTORS.market_name or ".ui-market__name"
MARKET_VALUE_SELECTOR = SELECTORS.market_value or ".ui-market__value"
MARKET_LOCKED_CLASS = SELECTORS.market_locked_class or "ui-market--locked"

NEXT_GOAL_TEXT = TEXTS.next_goal or "Следующий гол"
NEXT_GOAL_SEARCH_TEXT = TEXTS.next_goal_search or "следующий гол"
FIRST_HALF_1X2_TEXT = TEXTS.first_half_market or "1X2. 1-й тайм"
FIRST_HALF_DRAW_SELECTION_TEXT = TEXTS.draw_selection or "Ничья"

MARKET_KEYWORDS = tuple(TEXTS.market_response_keywords) or (
    "market",
    "odds",
    "coefficient",
    "event",
    "outcome",
    "goal",
    "гол",
)
SENSITIVE_KEYS = (
    "password",
    "passwd",
    "login",
    "token",
    "authorization",
    "cookie",
    "secret",
)

CANVAS_FALLBACK_STATUSES = frozenset(
    {
        "ELEMENT_NOT_READY",
        "MARKET_NOT_FOUND",
        "ODDS_NOT_FOUND",
        "MARKET_READ_ERROR",
    }
)


class MarketReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: str = "MARKET_READ_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.details = details or {}


class MarketNotAvailable(MarketReadError):
    pass


class MarketDomRequired(MarketReadError):
    pass


class CanvasCoefficientLocator:
    """Playwright-compatible click adapter for one OCR-detected canvas outcome."""

    def __init__(
        self,
        page: Page,
        region: dict[str, Any],
        canvas_shape: dict[str, Any],
    ) -> None:
        self.page = page
        self.region = dict(region)
        self.canvas_width = max(1.0, float(canvas_shape.get("width") or 1.0))
        self.canvas_height = max(1.0, float(canvas_shape.get("height") or 1.0))

    def _position(self, box: dict[str, float]) -> dict[str, float]:
        center_x = float(self.region["x"]) + float(self.region["width"]) / 2.0
        center_y = float(self.region["y"]) + float(self.region["height"]) / 2.0
        x = center_x * float(box["width"]) / self.canvas_width
        y = center_y * float(box["height"]) / self.canvas_height
        x = min(max(x, 1.0), max(1.0, float(box["width"]) - 1.0))
        y = min(max(y, 1.0), max(1.0, float(box["height"]) - 1.0))
        return {"x": x, "y": y}

    async def click(self, **kwargs: Any) -> None:
        canvas = self.page.locator(CANVAS_SELECTOR).first
        await canvas.wait_for(state="visible", timeout=int(kwargs.pop("timeout", 10_000)))
        box = await canvas.bounding_box()
        if box is None:
            raise MarketNotAvailable(
                "Canvas исчез перед кликом по коэффициенту.",
                status="CANVAS_NOT_READY",
                details={"source": "CANVAS_VISION"},
            )
        await canvas.scroll_into_view_if_needed()
        await canvas.click(position=self._position(box), **kwargs)


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


def _clean_text(value: str) -> str:
    return " ".join(value.replace("ё", "е").strip().lower().split())


def parse_next_goal_market_name(value: str) -> tuple[int, int] | None:
    """Return (team side, goal number) for a team outcome, ignoring no-goal rows."""
    normalized = _clean_text(value).replace("–", "-").replace("—", "-")
    if "не будет" in normalized or "гол" not in normalized:
        return None
    match = re.search(r"команда\s*([12])\s*-\s*(\d+)\s*-?\s*[йяе]?\s*гол", normalized)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_dom_odds(value: str) -> float:
    normalized = value.replace("\xa0", " ").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", normalized)
    if match is None:
        raise ValueError(f"Коэффициент не найден в {value!r}")
    odds = float(match.group(0))
    if odds <= 0:
        raise ValueError(f"Некорректный коэффициент: {value!r}")
    return odds


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 7:
        return "<max-depth>"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:250]:
            if any(marker in str(key).lower() for marker in SENSITIVE_KEYS):
                result[key] = "<redacted>"
            else:
                result[key] = _redact(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:250]]
    return value


def _selector_candidates(*values: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        value = (value or "").strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


async def _first_visible(page: Page, selectors: tuple[str, ...]) -> tuple[Any | None, str | None]:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return locator, selector
        except Exception:
            continue
    return None, None


MARKET_SEARCH_SCOPED_INPUT_SELECTORS = (
    '.game-panel__markets input.ui-search-default__input[placeholder="Поиск по рынкам"]',
    ".game-panel__markets input.ui-search-default__input",
    '.market-grid-game-panel__markets input.ui-search-default__input[placeholder="Поиск по рынкам"]',
    ".market-grid-game-panel__markets input.ui-search-default__input",
)
MARKET_SEARCH_FALLBACK_VISIBLE_INDEX = 1  # second visible search input, top to bottom


async def _visible_items(page: Page, selector: str) -> list[Any]:
    result: list[Any] = []
    try:
        locator = page.locator(selector)
        count = await locator.count()
    except Exception:
        return result
    for index in range(count):
        item = locator.nth(index)
        try:
            if await item.is_visible():
                result.append(item)
        except Exception:
            continue
    return result


async def _locate_market_search_input(
    page: Page,
    *,
    timeout_ms: int = 2_500,
) -> tuple[Any | None, str | None, int]:
    """Return the markets-panel input, never the upper event search by accident."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    generic_selectors = _selector_candidates(
        NEXT_GOAL_SEARCH_SELECTOR,
        'input.ui-search-default__input[placeholder="Поиск по рынкам"]',
        "input.ui-search-default__input",
        "input.game-search__input",
    )

    while True:
        # Prefer semantic scoping confirmed by the current match-page layout.
        for selector in MARKET_SEARCH_SCOPED_INPUT_SELECTORS:
            visible = await _visible_items(page, selector)
            if visible:
                return visible[0], selector, 0

        # Fallback for A/B layouts: the market filter is the second visible
        # ui-search input from top to bottom (the first belongs to the event bar).
        first_fallback: tuple[Any, str, int] | None = None
        for selector in generic_selectors:
            visible = await _visible_items(page, selector)
            if len(visible) > MARKET_SEARCH_FALLBACK_VISIBLE_INDEX:
                return (
                    visible[MARKET_SEARCH_FALLBACK_VISIBLE_INDEX],
                    selector,
                    MARKET_SEARCH_FALLBACK_VISIBLE_INDEX,
                )
            if visible and first_fallback is None:
                first_fallback = (visible[0], selector, 0)

        if loop.time() >= deadline:
            if first_fallback is not None:
                return first_fallback
            return None, None, 0
        await asyncio.sleep(0.1)


async def _paired_market_search_button(
    page: Page,
    search_input: Any,
    visible_position: int,
) -> tuple[Any | None, str | None]:
    # The button and input are rendered by the same ui-search-default component.
    # Resolve the button through that common ancestor so the upper search button
    # can never be clicked when the lower markets input was selected.
    try:
        container = search_input.locator(
            "xpath=ancestor::*[.//button[contains(@class,"
            "'ui-search-default__button')]][1]"
        )
        if await container.count():
            button = container.locator("button.ui-search-default__button").first
            if await button.count() and await button.is_visible():
                return button, "paired:button.ui-search-default__button"
    except Exception:
        pass

    # Defensive fallback for a future wrapper change: use the button with the
    # same visible top-to-bottom position as the selected input.
    for selector in _selector_candidates(
        MARKET_SEARCH_BUTTON_SELECTOR,
        "button.ui-search-default__button",
    ):
        visible = await _visible_items(page, selector)
        if visible:
            index = min(visible_position, len(visible) - 1)
            return visible[index], f"{selector}:visible:nth({index})"
    return None, None


async def _prepare_market_search(
    page: Page,
    search_text: str,
    logger: Logger | None = None,
) -> str:
    search_input, selector_used, visible_position = (
        await _locate_market_search_input(page)
    )
    if search_input is None or selector_used is None:
        raise MarketDomRequired(
            "Нижнее поле поиска рынков пока недоступно.",
            status="ELEMENT_NOT_READY",
            details={"source": "DOM_PLAYWRIGHT"},
        )

    await _log(
        logger,
        "MARKET_SEARCH_TARGET",
        (
            f"{selector_used}; visible_position={visible_position + 1}; "
            "scope=markets"
        ),
    )

    # Do not click and refill the same search on every polling attempt. The
    # click causes Vue to recreate/redraw the canvas and invalidates an OCR
    # confirmation that may already be in progress.
    try:
        current_value = (await search_input.input_value()).strip()
        canvas = page.locator(CANVAS_SELECTOR).first
        canvas_ready = await canvas.count() and await canvas.is_visible()
    except Exception:
        current_value = ""
        canvas_ready = False
    if (
        _clean_text(current_value) == _clean_text(search_text)
        and canvas_ready
    ):
        await _log(
            logger,
            "MARKET_SEARCH_REUSED",
            f"{selector_used}: search is already active; keeping current canvas",
        )
        return selector_used

    search_button, button_selector = await _paired_market_search_button(
        page,
        search_input,
        visible_position,
    )
    if search_button is not None:
        try:
            await search_button.scroll_into_view_if_needed()
            await search_button.click()
        except Exception as error:
            raise MarketDomRequired(
                "Кнопка нижнего поиска рынков найдена, но нажать её не удалось.",
                status="ELEMENT_NOT_READY",
                details={
                    "source": "DOM_PLAYWRIGHT",
                    "selector": button_selector,
                },
            ) from error
        await _log(
            logger,
            "MARKET_SEARCH_OPENED",
            f"Market search button clicked: {button_selector}",
        )

    # Vue may replace the node after the button click. Resolve the same lower
    # markets input again before filling it.
    refreshed_input, refreshed_selector, refreshed_position = (
        await _locate_market_search_input(page)
    )
    if refreshed_input is not None and refreshed_selector is not None:
        search_input = refreshed_input
        selector_used = refreshed_selector
        visible_position = refreshed_position

    await search_input.scroll_into_view_if_needed()
    await search_input.click()
    await search_input.fill("")
    await search_input.fill(search_text)

    actual_value = (await search_input.input_value()).strip()
    if _clean_text(actual_value) != _clean_text(search_text):
        raise MarketDomRequired(
            "Сайт не принял строку поиска рынка.",
            status="ELEMENT_NOT_READY",
            details={
                "source": "DOM_PLAYWRIGHT",
                "selector": selector_used,
                "visible_position": visible_position + 1,
                "expected": search_text,
                "actual": actual_value,
            },
        )

    await page.wait_for_timeout(250)
    await _log(
        logger,
        "MARKET_SEARCH_FILLED",
        (
            f"{selector_used}:visible:nth({visible_position}) = {search_text}; "
            "waiting for DOM/canvas render"
        ),
    )
    return selector_used


async def _market_label(button: Any) -> str:
    candidates: list[Any] = []
    try:
        candidates.append(button.locator(MARKET_NAME_SELECTOR).first)
    except Exception:
        pass
    candidates.append(button)

    for locator in candidates:
        try:
            if await locator.count() == 0:
                continue
        except Exception:
            continue

        for attribute in ("aria-label", "title", "data-original-title"):
            try:
                value = await locator.get_attribute(attribute)
            except Exception:
                value = None
            if value and value.strip():
                return value.strip()

        try:
            value = (await locator.inner_text()).strip()
        except Exception:
            value = ""
        if value:
            return value

    return ""


class ResponseMarketProbe:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.items: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    def _handler(self, response: Response) -> None:
        task = asyncio.create_task(self._inspect(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _inspect(self, response: Response) -> None:
        try:
            content_type = (await response.header_value("content-type") or "").lower()
            if "json" not in content_type:
                return
            payload = await response.json()
            serialized = json.dumps(payload, ensure_ascii=False).lower()
            url_lower = response.url.lower()
            if not any(key in serialized or key in url_lower for key in MARKET_KEYWORDS):
                return
            self.items.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "payload": _redact(payload),
                }
            )
        except Exception:
            return

    def start(self) -> None:
        self.page.on("response", self._handler)

    async def stop(self) -> list[dict[str, Any]]:
        self.page.remove_listener("response", self._handler)
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        return self.items


async def _probe_js_market_state(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(
        """() => {
            const roots = [
                ['__BETTING_APP__', window.__BETTING_APP__],
                ['__V3_HOST_APP__', window.__V3_HOST_APP__],
            ];
            const result = [];
            const seen = new WeakSet();
            const wanted = /market|odds|coefficient|outcome|goal|гол/i;
            const sensitive = /password|login|token|authorization|cookie|secret/i;
            const walk = (value, path, depth) => {
                if (result.length >= 150 || depth > 6 || value == null) return;
                if (typeof value !== 'object') return;
                if (seen.has(value)) return;
                seen.add(value);
                for (const [key, child] of Object.entries(value).slice(0, 300)) {
                    if (sensitive.test(key)) continue;
                    const childPath = `${path}.${key}`;
                    if (wanted.test(key)) {
                        let preview;
                        try {
                            preview = typeof child === 'object'
                                ? JSON.stringify(child).slice(0, 1500)
                                : String(child).slice(0, 1500);
                        } catch (_) {
                            preview = '<unserializable>';
                        }
                        result.push({path: childPath, preview});
                    }
                    walk(child, childPath, depth + 1);
                    if (result.length >= 150) break;
                }
            };
            for (const [name, root] of roots) walk(root, name, 0);
            return result;
        }"""
    )


async def open_additional_markets(
    page: Page, logger: Logger | None = None
) -> str | None:
    if await page.locator(GOALS_FILTER_SELECTOR).filter(has_text=GOALS_TEXT).count():
        return None
    await _log(logger, "OPENING_ADDITIONAL_MARKETS", "Opening additional markets")
    for selector in SELECTORS.additional_markets or (
        "button.dashboard-game__more",
        "button.dashboard-game-more",
    ):
        button = page.locator(selector).first
        if await button.count() == 0:
            continue
        try:
            await button.wait_for(state="visible", timeout=5_000)
            await button.scroll_into_view_if_needed()
            await button.click()
            await page.locator(GOALS_FILTER_SELECTOR).filter(
                has_text=GOALS_TEXT
            ).first.wait_for(state="visible", timeout=10_000)
            await _log(logger, "ADDITIONAL_MARKETS_CLICKED", selector)
            return selector
        except Exception:
            continue
    raise MarketNotAvailable(
        "Кнопка дополнительных рынков пока не готова.",
        status="ELEMENT_NOT_READY",
    )


async def open_goals_filter(page: Page, logger: Logger | None = None) -> dict[str, Any]:
    existing_canvas = page.locator(CANVAS_SELECTOR).first
    goals_switch = page.locator(GOALS_FILTER_SELECTOR).filter(has_text=GOALS_TEXT).first
    switch_class = await goals_switch.get_attribute("class") if await goals_switch.count() else ""
    aria_pressed = (
        await goals_switch.get_attribute("aria-pressed") if await goals_switch.count() else None
    )
    goals_active = aria_pressed == "true" or any(
        marker in (switch_class or "").lower()
        for marker in ("active", "selected", "checked")
    )
    if goals_active and await existing_canvas.count() and await existing_canvas.is_visible():
        box = await existing_canvas.bounding_box()
        if box:
            return {
                "selector": "already-open",
                "additional_selector": None,
                "canvas": {"width": round(box["width"]), "height": round(box["height"])},
                "network_candidates": [],
                "js_state_candidates": [],
            }

    additional_selector = await open_additional_markets(page, logger)
    await _log(logger, "OPENING_GOALS_FILTER", "Opening Goals filter")
    probe = ResponseMarketProbe(page)
    probe.start()
    try:
        goals_text = page.get_by_text(GOALS_TEXT, exact=True).first
        try:
            await goals_text.wait_for(state="visible", timeout=10_000)
            goals_filter = goals_text.locator(
                SELECTORS.goals_filter_ancestor_xpath
                or "xpath=ancestor::div[contains(@class,'game-toolbar-filter-switch')][1]"
            )
            selector_used = f'get_by_text("{GOALS_TEXT}", exact=True)'
        except Exception:
            goals_filter = page.locator(GOALS_FILTER_SELECTOR).filter(
                has_text=GOALS_TEXT
            ).first
            await goals_filter.wait_for(state="visible", timeout=10_000)
            selector_used = f'{GOALS_FILTER_SELECTOR}:has-text("{GOALS_TEXT}")'

        await goals_filter.scroll_into_view_if_needed()
        await goals_filter.click()
        await _log(logger, "GOALS_FILTER_OPENED", "Goals filter opened")

        canvas = page.locator(CANVAS_SELECTOR).first
        await canvas.wait_for(state="visible", timeout=10_000)
        box = await canvas.bounding_box()
        if box is None:
            raise MarketReadError(
                "Canvas не имеет bounding box.",
                status="CANVAS_NOT_VISIBLE",
            )
        await page.wait_for_timeout(100)
    finally:
        network_candidates = await probe.stop()

    js_state_candidates = await _probe_js_market_state(page)
    await _log(
        logger,
        "MARKET_CANVAS_DETECTED",
        f"Market canvas detected: {round(box['width'])}x{round(box['height'])}",
    )
    return {
        "selector": selector_used,
        "additional_selector": additional_selector,
        "canvas": {"width": round(box["width"]), "height": round(box["height"])},
        "network_candidates": network_candidates,
        "js_state_candidates": js_state_candidates,
    }


async def market_canvas_debug(
    page: Page,
    logger: Logger | None = None,
) -> dict[str, Any]:
    try:
        search_selector = await _prepare_market_search(
            page, NEXT_GOAL_SEARCH_TEXT, logger
        )
        canvas = page.locator(CANVAS_SELECTOR).first
        await canvas.wait_for(state="visible", timeout=5_000)
        box = await canvas.bounding_box()
        preparation = {
            "selector": search_selector,
            "additional_selector": None,
            "canvas": (
                {"width": round(box["width"]), "height": round(box["height"])}
                if box
                else None
            ),
            "network_candidates": [],
            "js_state_candidates": [],
        }
    except Exception:
        preparation = await open_goals_filter(page, logger)

    await _log(logger, "CANVAS_CAPTURE", "Capturing canvas")
    await _log(logger, "OCR_RUNNING", "Running OCR")
    try:
        analysis = await analyze_market_canvas(page)
    except CanvasVisionError as error:
        return {
            "ok": False,
            "status": error.status,
            "error": str(error),
            **preparation,
        }
    analysis.update(
        network_candidates=preparation["network_candidates"],
        js_state_candidates=preparation["js_state_candidates"],
        goals_selector=preparation["selector"],
    )
    await _log(
        logger,
        "OCR_NUMERIC_CANDIDATES",
        str([item["value"] for item in analysis.get("numbers", [])]),
    )
    await _log(logger, "OCR_ENGINE", str(analysis.get("ocr_backend") or "NONE"))
    return analysis


async def _read_next_goal_odds_dom(
    page: Page,
    team1: str,
    team2: str,
    next_goal_number: int,
    logger: Logger | None,
) -> NextGoalOdds:
    groups = page.locator(MARKET_GROUP_SELECTOR)
    try:
        await groups.first.wait_for(state="attached", timeout=2_500)
    except Exception as error:
        raise MarketNotAvailable(
            "Группа рынка «Следующий гол» пока не появилась.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT"},
        ) from error

    target_group = None
    for index in range(await groups.count()):
        group = groups.nth(index)
        title = group.locator(MARKET_GROUP_TITLE_SELECTOR).first
        if await title.count() == 0:
            continue
        try:
            title_text = await title.inner_text()
        except Exception:
            continue
        normalized_title = _clean_text(title_text)
        if normalized_title == _clean_text(NEXT_GOAL_TEXT) or _clean_text(
            NEXT_GOAL_TEXT
        ) in normalized_title:
            target_group = group
            break

    if target_group is None:
        raise MarketNotAvailable(
            "Точная группа рынка «Следующий гол» не найдена.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT"},
        )

    values: dict[int, float] = {}
    target_buttons: dict[int, Any] = {}
    locked_sides: set[int] = set()
    buttons = target_group.locator(MARKET_BUTTON_SELECTOR)

    for index in range(await buttons.count()):
        button = buttons.nth(index)
        parsed = parse_next_goal_market_name(await _market_label(button))
        if parsed is None:
            continue
        side, goal_number = parsed
        if goal_number != next_goal_number:
            continue

        classes = (await button.get_attribute("class") or "").lower()
        if await button.is_disabled() or MARKET_LOCKED_CLASS.lower() in classes:
            locked_sides.add(side)
            continue

        value_locator = button.locator(MARKET_VALUE_SELECTOR).first
        if await value_locator.count() == 0:
            continue
        try:
            values[side] = parse_dom_odds(await value_locator.inner_text())
            target_buttons[side] = button
        except ValueError:
            continue

    if 1 not in values or 2 not in values:
        status = "MARKET_LOCKED" if locked_sides else "ODDS_NOT_FOUND"
        raise MarketNotAvailable(
            f"Коэффициенты обеих команд для гола №{next_goal_number} пока недоступны.",
            status=status,
            details={
                "source": "DOM_PLAYWRIGHT",
                "next_goal_number": next_goal_number,
                "available_sides": sorted(values),
                "locked_sides": sorted(locked_sides),
            },
        )

    team1_odd = values[1]
    team2_odd = values[2]
    market = f"Следующий гол №{next_goal_number}"
    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(logger, "ODDS_SOURCE", "DOM / Playwright")
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")
    return NextGoalOdds(
        team1=float(team1_odd),
        team2=float(team2_odd),
        market=market,
        next_goal_number=next_goal_number,
        source="DOM_PLAYWRIGHT",
        ocr_backend=None,
        confidence=None,
        team1_locator=target_buttons[1],
        team2_locator=target_buttons[2],
    )


async def _read_next_goal_odds_canvas(
    page: Page,
    team1: str,
    team2: str,
    next_goal_number: int,
    logger: Logger | None,
) -> NextGoalOdds:
    canvas = page.locator(CANVAS_SELECTOR).first
    try:
        await canvas.wait_for(state="visible", timeout=3_500)
    except Exception as error:
        raise MarketNotAvailable(
            "DOM коэффициентов отсутствует и canvas рынка не найден.",
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_VISION"},
        ) from error

    await _log(
        logger,
        "ODDS_CANVAS_FALLBACK",
        "DOM coefficients unavailable; starting Canvas Vision",
    )
    try:
        analysis = await read_market_odds_from_canvas(
            page,
            expected_goal_number=next_goal_number,
        )
    except CanvasVisionError as error:
        raise MarketNotAvailable(
            str(error),
            status=error.status,
            details={"source": "CANVAS_VISION"},
        ) from error

    mapping = analysis.get("next_goal_mapping") or {}
    canvas_details = {
        "source": "CANVAS_VISION",
        "analysis_status": analysis.get("status"),
        "stability": analysis.get("stability"),
        "readings": analysis.get("readings"),
        "ocr_backend": analysis.get("ocr_backend"),
        "ocr_backends": analysis.get("ocr_backends"),
        "canvas": analysis.get("canvas"),
    }
    if mapping:
        await _log(
            logger,
            "CANVAS_ANALYSIS_RESULT",
            (
                f"status={analysis.get('status')}; "
                f"stability={analysis.get('stability')}; "
                f"goal={mapping.get('next_goal_number')}; "
                f"team1={(mapping.get('team1') or {}).get('value')}; "
                f"team2={(mapping.get('team2') or {}).get('value')}; "
                f"expected_goal={next_goal_number}"
            ),
        )
    if analysis.get("status") != "CANVAS_ANALYZED" or not mapping:
        raise MarketNotAvailable(
            "Canvas распознан, но коэффициенты нужного рынка не сопоставлены.",
            status=str(analysis.get("status") or "ODDS_MAPPING_UNCERTAIN"),
            details=canvas_details,
        )
    if analysis.get("stability") != "ODDS_CONFIRMED":
        raise MarketNotAvailable(
            "OCR дал нестабильные коэффициенты; ждём следующее чтение.",
            status="ODDS_UNSTABLE",
            details=canvas_details,
        )

    recognized_goal = mapping.get("next_goal_number")
    if recognized_goal is not None and int(recognized_goal) != int(next_goal_number):
        raise MarketNotAvailable(
            (
                f"Canvas показывает гол №{recognized_goal}, "
                f"а по счёту нужен гол №{next_goal_number}."
            ),
            status="STALE_MARKET",
            details={
                **canvas_details,
                "recognized_goal_number": recognized_goal,
                "expected_goal_number": next_goal_number,
                "recognized_team1": (mapping.get("team1") or {}).get("value"),
                "recognized_team2": (mapping.get("team2") or {}).get("value"),
            },
        )

    team1_region = mapping.get("team1") or {}
    team2_region = mapping.get("team2") or {}
    try:
        team1_odd = float(team1_region["value"])
        team2_odd = float(team2_region["value"])
    except (KeyError, TypeError, ValueError) as error:
        raise MarketNotAvailable(
            "OCR не вернул два валидных коэффициента.",
            status="ODDS_MAPPING_UNCERTAIN",
            details={"source": "CANVAS_VISION"},
        ) from error

    canvas_shape = analysis.get("canvas") or {}
    team1_locator = CanvasCoefficientLocator(page, team1_region, canvas_shape)
    team2_locator = CanvasCoefficientLocator(page, team2_region, canvas_shape)
    confidence = float(mapping.get("confidence") or 0.0)
    market = f"Следующий гол №{next_goal_number}"

    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(
        logger,
        "ODDS_SOURCE",
        f"CANVAS_VISION / {analysis.get('ocr_backend') or 'OCR'} / confidence={confidence:.3f}",
    )
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")

    return NextGoalOdds(
        team1=team1_odd,
        team2=team2_odd,
        market=market,
        next_goal_number=next_goal_number,
        source="CANVAS_VISION",
        ocr_backend=analysis.get("ocr_backend"),
        confidence=confidence,
        team1_locator=team1_locator,
        team2_locator=team2_locator,
    )


async def read_next_goal_odds(
    page: Page,
    team1: str,
    team2: str,
    score1: int,
    score2: int,
    logger: Logger | None = None,
    *,
    read_only: bool = False,
) -> NextGoalOdds:
    """Read next-goal odds with DOM-first and Canvas-Vision fallback."""
    next_goal_number = score1 + score2 + 1
    search_error: MarketReadError | None = None

    # Opening/filtering the market search is a read-only UI action and is
    # required in both DEMO and LIVE. Previously DEMO skipped this block, so
    # neither DOM nor Canvas Vision ever received the filtered next-goal market.
    _ = read_only  # Kept for API compatibility with existing callers.
    try:
        await _prepare_market_search(page, NEXT_GOAL_SEARCH_TEXT, logger)
    except MarketReadError as error:
        search_error = error
        await _log(
            logger,
            "MARKET_SEARCH_DOM_UNAVAILABLE",
            f"{error.status}: {error}",
        )

    try:
        return await _read_next_goal_odds_dom(
            page,
            team1,
            team2,
            next_goal_number,
            logger,
        )
    except MarketReadError as dom_error:
        if dom_error.status == "MARKET_LOCKED":
            raise

        fallback_status = (
            search_error.status if search_error is not None else dom_error.status
        )
        if (
            dom_error.status not in CANVAS_FALLBACK_STATUSES
            and fallback_status not in CANVAS_FALLBACK_STATUSES
        ):
            raise

        await _log(
            logger,
            "ODDS_DOM_FALLBACK",
            f"{dom_error.status}: {dom_error}; trying canvas",
        )
        try:
            return await _read_next_goal_odds_canvas(
                page,
                team1,
                team2,
                next_goal_number,
                logger,
            )
        except MarketReadError as canvas_error:
            details = {
                "source": "HYBRID_DOM_CANVAS",
                "dom_status": dom_error.status,
                "dom_error": str(dom_error),
                "canvas_status": canvas_error.status,
                "canvas_error": str(canvas_error),
                "canvas_details": canvas_error.details,
            }
            raise MarketNotAvailable(
                (
                    "Не удалось прочитать коэффициенты ни из DOM, ни через "
                    f"Canvas Vision: {canvas_error}"
                ),
                status=canvas_error.status,
                details=details,
            ) from canvas_error


async def read_first_half_draw_market(
    page: Page,
    logger: Logger | None = None,
) -> FirstHalfDrawMarket:
    """Read «Ничья» directly from «1X2. 1-й тайм» without using market search."""
    groups = page.locator(MARKET_GROUP_SELECTOR)
    try:
        await groups.first.wait_for(state="attached", timeout=5_000)
    except Exception as error:
        raise MarketNotAvailable(
            "Группа рынка «1X2. 1-й тайм» пока не появилась.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": False},
        ) from error

    target_group = None
    for index in range(await groups.count()):
        group = groups.nth(index)
        title = group.locator(MARKET_GROUP_TITLE_SELECTOR).first
        if await title.count() == 0:
            continue
        if _clean_text(await title.inner_text()) == _clean_text(FIRST_HALF_1X2_TEXT):
            target_group = group
            break
    if target_group is None:
        raise MarketNotAvailable(
            "Точная группа рынка «1X2. 1-й тайм» не найдена.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": False},
        )

    buttons = target_group.locator(MARKET_BUTTON_SELECTOR)
    draw_button = None
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if _clean_text(await _market_label(button)) == _clean_text(
            FIRST_HALF_DRAW_SELECTION_TEXT
        ):
            draw_button = button
            break
    if draw_button is None:
        raise MarketNotAvailable(
            "В рынке «1X2. 1-й тайм» не найден выбор «Ничья».",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )

    classes = (await draw_button.get_attribute("class") or "").lower()
    if await draw_button.is_disabled() or MARKET_LOCKED_CLASS.lower() in classes:
        raise MarketNotAvailable(
            "Рынок «Ничья в 1-м тайме» временно заблокирован.",
            status="MARKET_LOCKED",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )

    value = draw_button.locator(MARKET_VALUE_SELECTOR).first
    if await value.count() == 0:
        raise MarketNotAvailable(
            "Коэффициент «Ничья в 1-м тайме» пока недоступен.",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )
    try:
        odds = parse_dom_odds(await value.inner_text())
    except ValueError as error:
        raise MarketNotAvailable(
            "Коэффициент «Ничья в 1-м тайме» некорректен.",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        ) from error

    await _log(logger, "FIRST_HALF_DRAW_MARKET_FOUND", FIRST_HALF_1X2_TEXT)
    await _log(
        logger, "FIRST_HALF_DRAW_SELECTION_FOUND", FIRST_HALF_DRAW_SELECTION_TEXT
    )
    await _log(logger, "FIRST_HALF_DRAW_ODDS", str(odds))
    return FirstHalfDrawMarket(odds=odds, locator=draw_button)
