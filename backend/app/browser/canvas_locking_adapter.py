from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np
from playwright.async_api import Page

from backend.app.demo.models import NextGoalOdds
from xbet_config import SELECTORS

from . import market as hybrid_market
from .canvas_vision import (
    CANVAS_SELECTOR,
    CanvasVisionError,
    VISION,
    capture_market_canvas,
)


Logger = Callable[[str, str], Awaitable[Any]]


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


def _scaled_box(
    region: dict[str, Any],
    *,
    canvas_width: float,
    canvas_height: float,
    image_width: int,
    image_height: int,
) -> dict[str, float]:
    scale_x = image_width / canvas_width
    scale_y = image_height / canvas_height
    return {
        "x": float(region["x"]) * scale_x,
        "y": float(region["y"]) * scale_y,
        "width": float(region["width"]) * scale_x,
        "height": float(region["height"]) * scale_y,
    }


def detect_lock_marker(
    image: Any,
    outcome_region: dict[str, Any],
    *,
    side: int,
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any] | None:
    """Detect the bookmaker padlock drawn next to one team next-goal outcome."""
    if side not in {1, 2}:
        raise ValueError(f"Unsupported side: {side}")
    if image is None or image.size == 0 or canvas_width <= 0 or canvas_height <= 0:
        return None

    image_height, image_width = image.shape[:2]
    box = _scaled_box(
        outcome_region,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        image_width=image_width,
        image_height=image_height,
    )

    center_y = box["y"] + box["height"] / 2
    half_band = max(9, min(20, round(box["height"] * 0.90)))
    top = max(0, round(center_y - half_band))
    bottom = min(image_height, round(center_y + half_band + 1))

    # Team 1 is the left outcome column and Team 2 is the middle column.
    # Restrict the icon search to the matching team zone so a lock on
    # "Не будет N-го гола" cannot mark a team outcome as blocked.
    if side == 1:
        left = max(0, round(image_width * 0.01))
        right = min(round(box["x"] - 2), round(image_width * 0.24))
    else:
        left = max(0, round(image_width * 0.30))
        right = min(round(box["x"] - 2), round(image_width * 0.56))

    if right - left < 8 or bottom - top < 8:
        return None

    roi = image[top:bottom, left:right]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    background = float(np.percentile(gray, 88))
    dark_limit = int(min(180, background - 30))
    if dark_limit < 45:
        return None

    mask = np.where(gray <= dark_limit, 255, 0).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )

    icon_scale = max(10.0, box["height"])
    min_h = max(7, round(icon_scale * 0.45))
    max_h = min(32, max(18, round(icon_scale * 1.80)))
    min_w = max(5, round(icon_scale * 0.25))
    max_w = min(24, max(14, round(icon_scale * 1.10)))

    candidates: list[dict[str, Any]] = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if x <= 2 or x + width >= roi.shape[1] - 2:
            continue
        if not (min_w <= width <= max_w and min_h <= height <= max_h):
            continue

        aspect = width / height
        fill = area / float(width * height)
        if not (0.40 <= aspect <= 1.15 and 0.38 <= fill <= 0.92):
            continue

        component = (mask[y : y + height, x : x + width] > 0).astype(np.uint8)
        split = max(1, min(height - 1, round(height * 0.52)))
        upper_fill = float(component[:split].mean())
        lower_fill = float(component[split:].mean())
        bottom_rows = component[max(0, height - max(2, round(height * 0.32))) :]
        bottom_fill = float(bottom_rows.mean()) if bottom_rows.size else 0.0

        # Padlock silhouette: sparse shackle above a dense body.
        if not (0.05 <= upper_fill <= 0.72):
            continue
        if lower_fill < 0.64 or bottom_fill < 0.70:
            continue
        if lower_fill - upper_fill < 0.08:
            continue

        center_x = left + x + width / 2
        distance_to_odds = max(0.0, box["x"] - center_x)
        score = (
            lower_fill * 2.0
            + bottom_fill
            + (lower_fill - upper_fill)
            - min(distance_to_odds / max(1.0, image_width), 1.0)
        )
        candidates.append(
            {
                "x": left + x,
                "y": top + y,
                "width": width,
                "height": height,
                "fill": round(fill, 3),
                "upper_fill": round(upper_fill, 3),
                "lower_fill": round(lower_fill, 3),
                "bottom_fill": round(bottom_fill, 3),
                "background": round(background, 1),
                "dark_limit": dark_limit,
                "_score": score,
            }
        )

    if not candidates:
        return None

    best = max(candidates, key=lambda item: item["_score"])
    best.pop("_score", None)
    return best


def _locked_sides_from_image(
    image: Any,
    *,
    team1_region: dict[str, Any],
    team2_region: dict[str, Any],
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any]:
    markers: dict[str, Any] = {}
    for side, region in ((1, team1_region), (2, team2_region)):
        marker = detect_lock_marker(
            image,
            region,
            side=side,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )
        if marker is not None:
            markers[str(side)] = marker
    return {
        "locked_sides": sorted(int(side) for side in markers),
        "markers": markers,
    }


async def detect_locked_next_goal_sides(
    page: Page,
    *,
    team1_region: dict[str, Any],
    team2_region: dict[str, Any],
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any]:
    """Compatibility helper used by diagnostics/tests."""
    try:
        captured = await capture_market_canvas(page)
        result = _locked_sides_from_image(
            captured["image"],
            team1_region=team1_region,
            team2_region=team2_region,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )
        return {**result, "error": None}
    except Exception as error:
        return {
            "locked_sides": [],
            "markers": {},
            "error": f"{type(error).__name__}: {error}",
        }


async def _prepare_fast_canvas(page: Page, logger: Logger | None) -> bool:
    """Prepare the market search once; return True when the canvas is visible."""
    try:
        await hybrid_market._prepare_market_search(
            page,
            hybrid_market.NEXT_GOAL_SEARCH_TEXT,
            logger,
        )
    except hybrid_market.MarketReadError as error:
        await _log(
            logger,
            "FAST_MARKET_SEARCH_NOT_READY",
            f"{error.status}: {error}",
        )

    canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
    canvas = page.locator(canvas_selector).first
    try:
        return await canvas.count() > 0 and await canvas.is_visible()
    except Exception:
        return False


async def _analyze_single_frame(
    page: Page,
    *,
    expected_goal_number: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture exactly one canvas frame and map the requested row from it."""
    captured = await capture_market_canvas(page)
    image = captured["image"]

    analysis = await asyncio.to_thread(
        VISION.analyze_image,
        image,
        save=False,
        extended=False,
        expected_goal_number=expected_goal_number,
    )

    # This is not a second market reading. If the fast OCR layout pass misses
    # labels, re-process the exact same pixels once with the extended detector.
    if not analysis.get("next_goal_mapping"):
        analysis = await asyncio.to_thread(
            VISION.analyze_image,
            image,
            save=False,
            extended=True,
            expected_goal_number=expected_goal_number,
        )

    return captured, analysis


async def _read_fast_canvas_next_goal_odds(
    page: Page,
    team1: str,
    team2: str,
    score1: int,
    score2: int,
    logger: Logger | None,
) -> NextGoalOdds:
    next_goal_number = score1 + score2 + 1
    captured, analysis = await _analyze_single_frame(
        page,
        expected_goal_number=next_goal_number,
    )
    mapping = analysis.get("next_goal_mapping") or {}

    if analysis.get("status") != "CANVAS_ANALYZED" or not mapping:
        raise hybrid_market.MarketNotAvailable(
            "Один текущий кадр canvas не дал коэффициенты нужного рынка.",
            status="ODDS_NOT_FOUND",
            details={
                "source": "CANVAS_FAST_FRAME",
                "vision_status": analysis.get("status"),
                "next_goal_number": next_goal_number,
            },
        )

    recognized_goal = mapping.get("next_goal_number")
    if recognized_goal is not None and int(recognized_goal) != next_goal_number:
        raise hybrid_market.MarketNotAvailable(
            f"Canvas показывает гол №{recognized_goal}, ожидается №{next_goal_number}.",
            status="STALE_MARKET",
            details={
                "source": "CANVAS_FAST_FRAME",
                "recognized_goal_number": recognized_goal,
                "expected_goal_number": next_goal_number,
            },
        )

    team1_region = mapping.get("team1") or {}
    team2_region = mapping.get("team2") or {}
    canvas_meta = analysis.get("canvas") or {}
    canvas_width = float(canvas_meta.get("width") or captured.get("width") or 0)
    canvas_height = float(canvas_meta.get("height") or captured.get("height") or 0)

    try:
        team1_odd = float(team1_region["value"])
        team2_odd = float(team2_region["value"])
    except (KeyError, TypeError, ValueError) as error:
        raise hybrid_market.MarketNotAvailable(
            "Текущий кадр не содержит два валидных коэффициента.",
            status="ODDS_NOT_FOUND",
            details={"source": "CANVAS_FAST_FRAME"},
        ) from error

    lock_state = _locked_sides_from_image(
        captured["image"],
        team1_region=team1_region,
        team2_region=team2_region,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    if lock_state["locked_sides"]:
        await _log(
            logger,
            "CANVAS_MARKET_LOCKED",
            (
                f"Следующий гол №{next_goal_number}: "
                f"визуально заблокированы стороны {lock_state['locked_sides']}"
            ),
        )
        raise hybrid_market.MarketNotAvailable(
            f"Рынок следующего гола №{next_goal_number} заблокирован букмекером.",
            status="MARKET_LOCKED",
            details={
                "source": "CANVAS_FAST_VISUAL_LOCK",
                "next_goal_number": next_goal_number,
                "locked_sides": lock_state["locked_sides"],
                "market_available": True,
                "lock_markers": lock_state["markers"],
            },
        )

    confidence = float(mapping.get("confidence") or 0.0)
    canvas_shape = {"width": canvas_width, "height": canvas_height}
    market = f"Следующий гол №{next_goal_number}"

    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(
        logger,
        "ODDS_SOURCE",
        (
            "CANVAS_FAST_FRAME / one screenshot / no stability confirmation / "
            f"confidence={confidence:.3f}"
        ),
    )
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")

    return NextGoalOdds(
        team1=team1_odd,
        team2=team2_odd,
        market=market,
        next_goal_number=next_goal_number,
        source="CANVAS_FAST_FRAME",
        ocr_backend=analysis.get("ocr_backend"),
        confidence=confidence,
        team1_locator=hybrid_market.CanvasCoefficientLocator(
            page,
            team1_region,
            canvas_shape,
        ),
        team2_locator=hybrid_market.CanvasCoefficientLocator(
            page,
            team2_region,
            canvas_shape,
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
):
    """Fast strategy reader: one canvas frame is the virtual-bet decision frame."""
    _ = read_only  # API compatibility.

    if await _prepare_fast_canvas(page, logger):
        try:
            return await _read_fast_canvas_next_goal_odds(
                page,
                team1,
                team2,
                score1,
                score2,
                logger,
            )
        except CanvasVisionError as error:
            raise hybrid_market.MarketNotAvailable(
                str(error),
                status="ODDS_NOT_FOUND",
                details={
                    "source": "CANVAS_FAST_FRAME",
                    "vision_status": error.status,
                },
            ) from error

    # Compatibility only for an old non-canvas layout. The current canvas
    # layout never enters the old two/three-frame stability confirmation path.
    return await hybrid_market.read_next_goal_odds(
        page,
        team1,
        team2,
        score1,
        score2,
        logger,
        read_only=read_only,
    )
