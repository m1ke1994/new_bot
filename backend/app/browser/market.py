import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page, Response

from backend.app.demo.models import FirstHalfDrawMarket, NextGoalOdds, TotalEvenMarket
from xbet_config import SELECTORS, TEXTS

from .canvas_vision import (
    CANVAS_SELECTOR,
    CanvasVisionError,
    analyze_market_canvas,
    get_next_goal_odds,
)


Logger = Callable[[str, str], Awaitable[Any]]
GOALS_TEXT = TEXTS.goals
GOALS_FILTER_SELECTOR = SELECTORS.goals_filter
NEXT_GOAL_SEARCH_SELECTOR = SELECTORS.market_search
MARKET_GROUP_SELECTOR = SELECTORS.market_group
MARKET_GROUP_TITLE_SELECTOR = SELECTORS.market_group_title
MARKET_BUTTON_SELECTOR = SELECTORS.market_button
MARKET_NAME_SELECTOR = SELECTORS.market_name
MARKET_VALUE_SELECTOR = SELECTORS.market_value
NEXT_GOAL_TEXT = TEXTS.next_goal
TOTAL_EVEN_TEXT = TEXTS.total_even
TOTAL_EVEN_SEARCH_TEXT = TEXTS.total_even_search
TOTAL_EVEN_SELECTION_TEXT = TEXTS.affirmative_selection
FIRST_HALF_1X2_TEXT = TEXTS.first_half_market
FIRST_HALF_DRAW_SELECTION_TEXT = TEXTS.draw_selection
MARKET_KEYWORDS = TEXTS.market_response_keywords
SENSITIVE_KEYS = (
    "password",
    "passwd",
    "login",
    "token",
    "authorization",
    "cookie",
    "secret",
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


class CanvasOutcomeLocator:
    """Playwright-like clickable adapter for an OCR-detected outcome inside canvas.

    LiveExecutor intentionally knows nothing about canvas coordinates. It only needs an
    object exposing ``click()``. Keeping that contract lets DOM and canvas markets share
    the same LIVE placement flow without changing strategy or progression logic.
    """

    def __init__(
        self,
        page: Page,
        region: dict[str, Any],
        *,
        side: int,
        expected_odds: float,
    ) -> None:
        self.page = page
        self.region = dict(region)
        self.side = side
        self.expected_odds = float(expected_odds)

    async def click(self, **kwargs: Any) -> None:
        timeout = kwargs.get("timeout", 5_000)
        canvas = self.page.locator(CANVAS_SELECTOR).first
        await canvas.wait_for(state="visible", timeout=timeout)
        box = await canvas.bounding_box()
        if box is None:
            raise RuntimeError("Canvas коэффициентов не имеет bounding box перед кликом.")

        try:
            x = float(self.region["x"]) + float(self.region["width"]) / 2.0
            y = float(self.region["y"]) + float(self.region["height"]) / 2.0
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("OCR не сохранил координаты выбранного коэффициента.") from error

        if x < 0 or y < 0 or x > float(box["width"]) or y > float(box["height"]):
            raise RuntimeError(
                f"OCR-координата исхода вышла за пределы canvas: side={self.side} x={x:.1f} y={y:.1f}."
            )

        await canvas.click(position={"x": x, "y": y}, timeout=timeout)


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
    for selector in SELECTORS.additional_markets:
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
            goals_filter = goals_text.locator(SELECTORS.goals_filter_ancestor_xpath)
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


async def _read_next_goal_odds_from_canvas(
    page: Page,
    team1: str,
    team2: str,
    logger: Logger | None,
) -> NextGoalOdds:
    try:
        result = await get_next_goal_odds(page, team1, team2)
    except CanvasVisionError as error:
        raise MarketNotAvailable(
            str(error),
            status=error.status,
            details={"source": "CANVAS_OCR"},
        ) from error

    if not result.get("ok"):
        status = str(result.get("status") or "ODDS_NOT_FOUND")
        raise MarketNotAvailable(
            f"Canvas/OCR не подтвердил коэффициенты рынка: {status}.",
            status=status,
            details={
                "source": "CANVAS_OCR",
                "confidence": result.get("confidence"),
            },
        )

    try:
        team1_odd = float(result["team1"]["odds"])
        team2_odd = float(result["team2"]["odds"])
        next_goal_number = int(result["next_goal_number"])
    except (KeyError, TypeError, ValueError) as error:
        raise MarketNotAvailable(
            "Canvas/OCR вернул неполную структуру коэффициентов.",
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_OCR"},
        ) from error

    analysis = result.get("analysis") or {}
    mapping = analysis.get("next_goal_mapping") or {}
    team1_region = mapping.get("team1") or {}
    team2_region = mapping.get("team2") or {}
    if not team1_region or not team2_region:
        raise MarketNotAvailable(
            "Canvas/OCR определил коэффициенты, но не сохранил координаты исходов.",
            status="ODDS_MAPPING_UNCERTAIN",
            details={"source": "CANVAS_OCR"},
        )

    market = f"Следующий гол №{next_goal_number}"
    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(logger, "ODDS_SOURCE", "Canvas / OCR")
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")

    return NextGoalOdds(
        team1=team1_odd,
        team2=team2_odd,
        market=market,
        next_goal_number=next_goal_number,
        source="CANVAS_OCR",
        ocr_backend=result.get("ocr_backend"),
        confidence=result.get("confidence"),
        team1_locator=CanvasOutcomeLocator(
            page, team1_region, side=1, expected_odds=team1_odd
        ),
        team2_locator=CanvasOutcomeLocator(
            page, team2_region, side=2, expected_odds=team2_odd
        ),
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
    """Read next-goal odds from the current site.

    New site builds draw outcome values inside canvas, while older mirrors still expose
    normal market buttons. We therefore preserve the old DOM reader and prefer the
    canvas reader whenever the configured market canvas is visible.
    """
    next_goal_number = score1 + score2 + 1

    if not read_only:
        search_input = page.locator(NEXT_GOAL_SEARCH_SELECTOR).first
        try:
            await search_input.wait_for(state="visible", timeout=5_000)
            current = await search_input.input_value()
            if _clean_text(current) != _clean_text(TEXTS.next_goal_search):
                await search_input.fill(TEXTS.next_goal_search)
                await page.wait_for_timeout(100)
        except Exception as error:
            await _log(logger, "MARKET_SEARCH_NOT_READY", str(error))

    canvas_error: MarketReadError | None = None
    try:
        canvas = page.locator(CANVAS_SELECTOR).first
        if await canvas.count() and await canvas.is_visible():
            try:
                return await _read_next_goal_odds_from_canvas(page, team1, team2, logger)
            except MarketReadError as error:
                canvas_error = error
                await _log(
                    logger,
                    "CANVAS_ODDS_NOT_READY",
                    f"{error.status}: {error}; пробуем DOM fallback",
                )
    except Exception as error:
        await _log(logger, "CANVAS_VISIBILITY_CHECK_FAILED", str(error))

    groups = page.locator(MARKET_GROUP_SELECTOR)
    try:
        await groups.first.wait_for(state="attached", timeout=2_000)
    except Exception as error:
        if canvas_error is not None:
            raise canvas_error
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
        if _clean_text(await title.inner_text()) == _clean_text(NEXT_GOAL_TEXT):
            target_group = group
            break
    if target_group is None:
        if canvas_error is not None:
            raise canvas_error
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
        name_locator = button.locator(MARKET_NAME_SELECTOR).first
        if await name_locator.count() == 0:
            continue
        parsed = parse_next_goal_market_name(await name_locator.inner_text())
        if parsed is None:
            continue
        side, goal_number = parsed
        if goal_number != next_goal_number:
            continue

        classes = (await button.get_attribute("class") or "").lower()
        if await button.is_disabled() or SELECTORS.market_locked_class.lower() in classes:
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
        if canvas_error is not None and not locked_sides:
            raise canvas_error
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


async def read_total_even_market(
    page: Page,
    logger: Logger | None = None,
) -> TotalEvenMarket:
    """Read «Тотал чёт — Да» from its exact DOM group without global text lookup."""
    search_input = page.locator(NEXT_GOAL_SEARCH_SELECTOR).first
    try:
        await search_input.wait_for(state="visible", timeout=5_000)
        await search_input.fill(TOTAL_EVEN_SEARCH_TEXT)
    except Exception as error:
        raise MarketDomRequired(
            "Поле поиска рынков пока недоступно.",
            status="ELEMENT_NOT_READY",
            details={"source": "DOM_PLAYWRIGHT"},
        ) from error

    groups = page.locator(MARKET_GROUP_SELECTOR)
    try:
        await groups.first.wait_for(state="attached", timeout=5_000)
    except Exception as error:
        raise MarketNotAvailable(
            "Группа рынка «Тотал чёт» пока не появилась.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT"},
        ) from error

    target_group = None
    for index in range(await groups.count()):
        group = groups.nth(index)
        title = group.locator(MARKET_GROUP_TITLE_SELECTOR).first
        if await title.count() == 0:
            continue
        if _clean_text(await title.inner_text()) == _clean_text(TOTAL_EVEN_TEXT):
            target_group = group
            break
    if target_group is None:
        raise MarketNotAvailable(
            "Точная группа рынка «Тотал чёт» не найдена.",
            status="MARKET_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT"},
        )

    buttons = target_group.locator(MARKET_BUTTON_SELECTOR)
    yes_button = None
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        name = button.locator(MARKET_NAME_SELECTOR).first
        if await name.count() == 0:
            continue
        if _clean_text(await name.inner_text()) == _clean_text(TOTAL_EVEN_SELECTION_TEXT):
            yes_button = button
            break
    if yes_button is None:
        raise MarketNotAvailable(
            "В рынке «Тотал чёт» не найден выбор «Да».",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )

    classes = (await yes_button.get_attribute("class") or "").lower()
    if await yes_button.is_disabled() or SELECTORS.market_locked_class.lower() in classes:
        raise MarketNotAvailable(
            "Рынок «Тотал чёт — Да» временно заблокирован.",
            status="MARKET_LOCKED",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )

    value = yes_button.locator(MARKET_VALUE_SELECTOR).first
    if await value.count() == 0:
        raise MarketNotAvailable(
            "Коэффициент «Тотал чёт — Да» пока недоступен.",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        )
    try:
        odds = parse_dom_odds(await value.inner_text())
    except ValueError as error:
        raise MarketNotAvailable(
            "Коэффициент «Тотал чёт — Да» некорректен.",
            status="ODDS_NOT_FOUND",
            details={"source": "DOM_PLAYWRIGHT", "market_available": True},
        ) from error

    await _log(logger, "TOTAL_EVEN_MARKET_FOUND", TOTAL_EVEN_TEXT)
    await _log(logger, "TOTAL_EVEN_SELECTION_FOUND", TOTAL_EVEN_SELECTION_TEXT)
    await _log(logger, "TOTAL_EVEN_ODDS", str(odds))
    return TotalEvenMarket(odds=odds, locator=yes_button)


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
        name = button.locator(MARKET_NAME_SELECTOR).first
        if await name.count() == 0:
            continue
        if _clean_text(await name.inner_text()) == _clean_text(
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
    if await draw_button.is_disabled() or SELECTORS.market_locked_class.lower() in classes:
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
