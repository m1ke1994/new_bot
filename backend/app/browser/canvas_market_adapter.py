from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page

from backend.app.demo.models import NextGoalOdds
from xbet_config import SELECTORS, TEXTS

from . import market as dom_market
from .canvas_vision import CANVAS_SELECTOR, CanvasVisionError, get_next_goal_odds


Logger = Callable[[str, str], Awaitable[Any]]


class CanvasCoefficientLocator:
    """Playwright-like click adapter for an OCR-mapped outcome inside market canvas."""

    def __init__(
        self,
        page: Page,
        *,
        outcome_box: dict[str, Any],
        canvas_width: float,
        canvas_height: float,
    ) -> None:
        self.page = page
        self.outcome_box = dict(outcome_box)
        self.canvas_width = float(canvas_width)
        self.canvas_height = float(canvas_height)

    async def click(self, **kwargs: Any) -> None:
        canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
        canvas = self.page.locator(canvas_selector).first
        await canvas.wait_for(state="visible", timeout=10_000)
        css_box = await canvas.bounding_box()
        if css_box is None:
            raise RuntimeError("CANVAS_CLICK_FAILED: canvas has no bounding box")
        if self.canvas_width <= 0 or self.canvas_height <= 0:
            raise RuntimeError("CANVAS_CLICK_FAILED: invalid OCR canvas size")

        center_x = float(self.outcome_box["x"]) + float(self.outcome_box["width"]) / 2
        center_y = float(self.outcome_box["y"]) + float(self.outcome_box["height"]) / 2
        position = {
            "x": center_x * float(css_box["width"]) / self.canvas_width,
            "y": center_y * float(css_box["height"]) / self.canvas_height,
        }

        click_kwargs = dict(kwargs)
        click_kwargs["position"] = position
        await canvas.click(**click_kwargs)


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


async def _is_visible(page: Page, selector: str) -> bool:
    if not selector:
        return False
    try:
        locator = page.locator(selector).first
        return await locator.count() > 0 and await locator.is_visible()
    except Exception:
        return False


async def _prepare_canvas(page: Page, logger: Logger | None) -> None:
    """Expose the next-goal market without relying on obsolete outcome DOM nodes."""
    search_selector = SELECTORS.market_search
    if await _is_visible(page, search_selector):
        search = page.locator(search_selector).first
        try:
            current = await search.input_value()
        except Exception:
            current = ""
        wanted = TEXTS.next_goal_search
        if wanted.casefold() not in current.casefold():
            await search.fill(wanted)
            await _log(logger, "NEXT_GOAL_CANVAS_SEARCH", f"search={wanted!r}")
        canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
        canvas = page.locator(canvas_selector).first
        try:
            await canvas.wait_for(state="visible", timeout=3_000)
            return
        except Exception:
            pass

    canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
    if await _is_visible(page, canvas_selector):
        return

    # Older layouts expose the same canvas through the Goals filter.
    await dom_market.open_goals_filter(page, logger)
    canvas = page.locator(canvas_selector).first
    await canvas.wait_for(state="visible", timeout=10_000)


async def _read_canvas_odds(
    page: Page,
    team1: str,
    team2: str,
    score1: int,
    score2: int,
    logger: Logger | None,
) -> NextGoalOdds:
    expected_goal = score1 + score2 + 1
    try:
        await _prepare_canvas(page, logger)
        payload = await get_next_goal_odds(page, team1, team2)
    except CanvasVisionError as error:
        raise dom_market.MarketNotAvailable(
            str(error),
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_OCR", "vision_status": error.status},
        ) from error
    except dom_market.MarketReadError:
        raise
    except Exception as error:
        raise dom_market.MarketNotAvailable(
            f"Canvas/OCR reader failed: {error}",
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_OCR"},
        ) from error

    if not payload.get("ok"):
        raise dom_market.MarketNotAvailable(
            "Canvas/OCR пока не подтвердил коэффициенты обеих команд.",
            status="ODDS_NOT_FOUND",
            details={
                "source": "CANVAS_OCR",
                "vision_status": payload.get("status"),
                "next_goal_number": expected_goal,
            },
        )

    goal_number = payload.get("next_goal_number")
    if goal_number is not None and int(goal_number) != expected_goal:
        raise dom_market.MarketNotAvailable(
            f"Canvas/OCR вернул рынок гола №{goal_number}, ожидается №{expected_goal}.",
            status="ODDS_NOT_FOUND",
            details={
                "source": "CANVAS_OCR",
                "vision_status": "STALE_MARKET",
                "next_goal_number": goal_number,
                "expected_goal_number": expected_goal,
            },
        )

    analysis = payload.get("analysis") or {}
    mapping = analysis.get("next_goal_mapping") or {}
    canvas_meta = analysis.get("canvas") or {}
    team1_box = mapping.get("team1")
    team2_box = mapping.get("team2")
    canvas_width = float(canvas_meta.get("width") or 0)
    canvas_height = float(canvas_meta.get("height") or 0)

    if not team1_box or not team2_box or canvas_width <= 0 or canvas_height <= 0:
        raise dom_market.MarketNotAvailable(
            "Canvas/OCR распознал коэффициенты, но не сохранил координаты исходов.",
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_OCR", "vision_status": "MAPPING_INCOMPLETE"},
        )

    team1_odd = float(payload["team1"]["odds"])
    team2_odd = float(payload["team2"]["odds"])
    market = str(payload.get("market") or f"Следующий гол №{expected_goal}")
    confidence = payload.get("confidence")

    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(
        logger,
        "ODDS_SOURCE",
        f"CANVAS_OCR / {payload.get('ocr_backend') or 'unknown'} / confidence={confidence}",
    )
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")

    return NextGoalOdds(
        team1=team1_odd,
        team2=team2_odd,
        market=market,
        next_goal_number=expected_goal,
        source="CANVAS_OCR",
        ocr_backend=payload.get("ocr_backend"),
        confidence=float(confidence) if confidence is not None else None,
        team1_locator=CanvasCoefficientLocator(
            page,
            outcome_box=team1_box,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        ),
        team2_locator=CanvasCoefficientLocator(
            page,
            outcome_box=team2_box,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
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
    """
    Compatibility reader used by the strategy engine.

    New bookmaker layouts render outcome labels/odds in canvas. Prefer the
    configured canvas/search path when present, but keep the old DOM reader as
    a fallback for layouts that still expose real market buttons.
    """
    canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
    canvas_or_search_present = (
        await _is_visible(page, canvas_selector)
        or await _is_visible(page, SELECTORS.market_search)
    )

    if canvas_or_search_present:
        try:
            return await _read_canvas_odds(
                page, team1, team2, score1, score2, logger
            )
        except dom_market.MarketReadError as canvas_error:
            # If the page still has true DOM market buttons, preserve the old
            # reader as a compatibility fallback. Otherwise keep retrying OCR.
            market_button_selector = SELECTORS.market_button or dom_market.MARKET_BUTTON_SELECTOR
            try:
                has_dom_market = await page.locator(market_button_selector).count() > 0
            except Exception:
                has_dom_market = False
            if not has_dom_market:
                raise
            await _log(
                logger,
                "CANVAS_ODDS_FALLBACK_TO_DOM",
                f"{canvas_error.status}: {canvas_error}",
            )

    return await dom_market.read_next_goal_odds(
        page,
        team1,
        team2,
        score1,
        score2,
        logger,
        read_only=read_only,
    )
