import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page, Response

from backend.app.demo.models import NextGoalOdds

from .canvas_vision import (
    CANVAS_SELECTOR,
    CanvasVisionError,
    analyze_market_canvas,
    get_next_goal_odds,
)


Logger = Callable[[str, str], Awaitable[Any]]
GOALS_TEXT = "Голы"
GOALS_FILTER_SELECTOR = ".game-toolbar-filter-switch"
MARKET_KEYWORDS = (
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


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


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
    for selector in ("button.dashboard-game__more", "button.dashboard-game-more"):
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
                "xpath=ancestor::div[contains(@class,'game-toolbar-filter-switch')][1]"
            )
            selector_used = 'get_by_text("Голы", exact=True)'
        except Exception:
            goals_filter = page.locator(GOALS_FILTER_SELECTOR).filter(
                has_text=GOALS_TEXT
            ).first
            await goals_filter.wait_for(state="visible", timeout=10_000)
            selector_used = f'{GOALS_FILTER_SELECTOR}:has-text("Голы")'

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


async def read_next_goal_odds(
    page: Page,
    team1: str,
    team2: str,
    logger: Logger | None = None,
) -> NextGoalOdds:
    preparation = await open_goals_filter(page, logger)
    await _log(logger, "CANVAS_CAPTURE", "Capturing canvas")
    await _log(logger, "OCR_RUNNING", "Running OCR")
    try:
        result = await get_next_goal_odds(page, team1, team2)
    except CanvasVisionError as error:
        raise MarketReadError(
            str(error),
            status=error.status,
            details={"source": "CANVAS_OCR", **preparation},
        ) from error

    result["network_candidates"] = preparation["network_candidates"]
    result["js_state_candidates"] = preparation["js_state_candidates"]
    result["goals_selector"] = preparation["selector"]
    await _log(
        logger,
        "OCR_NUMERIC_CANDIDATES",
        str([item["value"] for item in result.get("analysis", {}).get("numbers", [])]),
    )

    analysis = result.get("analysis", {})
    await _log(logger, "OCR_ENGINE", str(analysis.get("ocr_backend") or "NONE"))

    if not result.get("ok"):
        raise MarketReadError(
            f"{result.get('status')}: коэффициенты нельзя использовать автоматически.",
            status=result.get("status", "ODDS_MAPPING_UNCERTAIN"),
            details=result,
        )

    team1_odd = result["team1"]["odds"]
    team2_odd = result["team2"]["odds"]
    await _log(logger, "NEXT_GOAL_MARKET_FOUND", result.get("market", "Следующий гол"))
    await _log(logger, "ODDS_MAPPED", "Odds candidates mapped")
    await _log(logger, "TEAM1_ODDS", f"Team1 odds: {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Team2 odds: {team2_odd}")
    await _log(logger, "ODDS_SOURCE", f"Odds source: {result['source']}")
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")
    return NextGoalOdds(
        team1=float(team1_odd),
        team2=float(team2_odd),
        market=result.get("market", "Следующий гол"),
        next_goal_number=result.get("next_goal_number"),
        source=result.get("source", "CANVAS_OCR"),
        ocr_backend=result.get("ocr_backend"),
        confidence=result.get("confidence"),
    )
