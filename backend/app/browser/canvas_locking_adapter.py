from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import cv2
import numpy as np
from playwright.async_api import Page

from xbet_config import SELECTORS

from . import market as hybrid_market
from .canvas_vision import (
    CANVAS_SELECTOR,
    VISION,
    CanvasVisionError,
    capture_market_canvas,
    decode_canvas_image,
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
    """Detect the small bookmaker padlock next to one team outcome.

    The search is deliberately kept close to the mapped coefficient. The old
    wide Team-2 scan could mistake ordinary text/icons for a padlock and then
    suppress otherwise valid coefficients.
    """
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

    side_left = round(image_width * (0.01 if side == 1 else 0.30))
    side_right = round(image_width * (0.24 if side == 1 else 0.56))
    proximity = max(80, round(image_width * 0.14))
    left = max(0, side_left, round(box["x"] - proximity))
    right = min(round(box["x"] - 2), side_right)

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
    max_h = min(30, max(17, round(icon_scale * 1.65)))
    min_w = max(5, round(icon_scale * 0.25))
    max_w = min(20, max(13, round(icon_scale * 0.95)))

    candidates: list[dict[str, Any]] = []
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        if x <= 1 or x + width >= roi.shape[1] - 1:
            continue
        if not (min_w <= width <= max_w and min_h <= height <= max_h):
            continue

        aspect = width / height
        fill = area / float(width * height)
        if not (0.42 <= aspect <= 1.05 and 0.42 <= fill <= 0.90):
            continue

        component = (mask[y : y + height, x : x + width] > 0).astype(np.uint8)
        split = max(1, min(height - 1, round(height * 0.52)))
        upper_fill = float(component[:split].mean())
        lower_fill = float(component[split:].mean())
        bottom_rows = component[max(0, height - max(2, round(height * 0.32))) :]
        bottom_fill = float(bottom_rows.mean()) if bottom_rows.size else 0.0

        if not (0.05 <= upper_fill <= 0.68):
            continue
        if lower_fill < 0.68 or bottom_fill < 0.74:
            continue
        if lower_fill - upper_fill < 0.12:
            continue

        center_x = left + x + width / 2
        distance_to_odds = max(0.0, box["x"] - center_x)
        if distance_to_odds > proximity:
            continue

        score = (
            lower_fill * 2.0
            + bottom_fill
            + (lower_fill - upper_fill)
            - distance_to_odds / max(1.0, proximity)
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
                "distance_to_odds": round(distance_to_odds, 1),
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
    """Fallback lock scan when odds came from the reliable multi-frame path."""
    try:
        canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
        canvas = page.locator(canvas_selector).first
        await canvas.wait_for(state="visible", timeout=750)
        image_bytes = await canvas.screenshot(type="png")
        image = decode_canvas_image(image_bytes)
        result = _locked_sides_from_image(
            image,
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


async def _single_frame_canvas_read(
    page: Page,
    team1: str,
    team2: str,
    next_goal_number: int,
    logger: Logger | None,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Read both odds and visual locks from one current Canvas screenshot.

    This is the hot DEMO path: no second screenshot and no confirmation frame.
    If one-frame OCR cannot map the expected row, callers immediately fall back
    to the existing reliable multi-frame reader instead of inventing values.
    """
    try:
        captured = await capture_market_canvas(page)
        analysis = await asyncio.to_thread(
            VISION.analyze_image,
            captured["image"],
            save=False,
            extended=False,
            expected_goal_number=next_goal_number,
        )
    except CanvasVisionError as error:
        await _log(
            logger,
            "ODDS_CANVAS_SINGLE_FRAME_MISS",
            f"{error.status}: {error}; fallback to reliable reader",
        )
        return None, None
    except Exception as error:
        await _log(
            logger,
            "ODDS_CANVAS_SINGLE_FRAME_MISS",
            f"{type(error).__name__}: {error}; fallback to reliable reader",
        )
        return None, None

    mapping = analysis.get("next_goal_mapping") or {}
    if analysis.get("status") != "CANVAS_ANALYZED" or not mapping:
        await _log(
            logger,
            "ODDS_CANVAS_SINGLE_FRAME_MISS",
            (
                f"status={analysis.get('status')}; goal={next_goal_number}; "
                "fallback to reliable reader"
            ),
        )
        return None, None

    recognized_goal = mapping.get("next_goal_number")
    if recognized_goal is not None and int(recognized_goal) != int(next_goal_number):
        await _log(
            logger,
            "ODDS_CANVAS_SINGLE_FRAME_MISS",
            (
                f"stale goal={recognized_goal}; expected={next_goal_number}; "
                "fallback to reliable reader"
            ),
        )
        return None, None

    team1_region = mapping.get("team1") or {}
    team2_region = mapping.get("team2") or {}
    try:
        team1_odd = float(team1_region["value"])
        team2_odd = float(team2_region["value"])
    except (KeyError, TypeError, ValueError):
        await _log(
            logger,
            "ODDS_CANVAS_SINGLE_FRAME_MISS",
            "One-frame OCR did not return two valid odds; fallback to reliable reader",
        )
        return None, None

    canvas_shape = analysis.get("canvas") or {
        "width": captured["width"],
        "height": captured["height"],
    }
    canvas_width = max(1.0, float(canvas_shape.get("width") or captured["width"]))
    canvas_height = max(1.0, float(canvas_shape.get("height") or captured["height"]))
    team1_locator = hybrid_market.CanvasCoefficientLocator(
        page,
        team1_region,
        canvas_shape,
    )
    team2_locator = hybrid_market.CanvasCoefficientLocator(
        page,
        team2_region,
        canvas_shape,
    )
    confidence = float(mapping.get("confidence") or 0.0)
    market = f"Следующий гол №{next_goal_number}"

    lock_state = _locked_sides_from_image(
        captured["image"],
        team1_region=team1_region,
        team2_region=team2_region,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    lock_state = {**lock_state, "error": None}

    await _log(
        logger,
        "ODDS_CANVAS_SINGLE_FRAME",
        (
            f"goal={next_goal_number}; odds={team1_odd}/{team2_odd}; "
            f"locked_sides={lock_state['locked_sides']}; "
            f"ocr_latency={analysis.get('latency_seconds')}s"
        ),
    )
    await _log(logger, "NEXT_GOAL_MARKET_FOUND", market)
    await _log(logger, "TEAM1_ODDS", f"Команда 1 / {team1} = {team1_odd}")
    await _log(logger, "TEAM2_ODDS", f"Команда 2 / {team2} = {team2_odd}")
    await _log(
        logger,
        "ODDS_SOURCE",
        (
            f"CANVAS_VISION_SINGLE_FRAME / "
            f"{analysis.get('ocr_backend') or 'OCR'} / confidence={confidence:.3f}"
        ),
    )
    await _log(logger, "ODDS_READY", f"{team1_odd} / {team2_odd}")

    odds = hybrid_market.NextGoalOdds(
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
    return odds, lock_state


async def _read_odds_canvas_first(
    page: Page,
    team1: str,
    team2: str,
    score1: int,
    score2: int,
    logger: Logger | None,
    *,
    read_only: bool,
) -> tuple[Any, dict[str, Any] | None]:
    """Prefer one-frame Canvas; keep the old hybrid reader as a safe fallback."""
    next_goal_number = score1 + score2 + 1
    try:
        await hybrid_market._prepare_market_search(
            page,
            hybrid_market.NEXT_GOAL_SEARCH_TEXT,
            logger,
        )
    except hybrid_market.MarketReadError:
        odds = await hybrid_market.read_next_goal_odds(
            page,
            team1,
            team2,
            score1,
            score2,
            logger,
            read_only=read_only,
        )
        return odds, None

    canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
    canvas = page.locator(canvas_selector).first
    try:
        canvas_ready = bool(await canvas.count() and await canvas.is_visible())
    except Exception:
        canvas_ready = False

    if not canvas_ready:
        odds = await hybrid_market.read_next_goal_odds(
            page,
            team1,
            team2,
            score1,
            score2,
            logger,
            read_only=read_only,
        )
        return odds, None

    await _log(
        logger,
        "ODDS_CANVAS_FAST_PATH",
        (
            f"Canvas visible; one-frame odds+lock read for goal №{next_goal_number}; "
            "DOM timeout and confirmation screenshot skipped"
        ),
    )

    odds, lock_state = await _single_frame_canvas_read(
        page,
        team1,
        team2,
        next_goal_number,
        logger,
    )
    if odds is not None:
        return odds, lock_state

    await _log(
        logger,
        "ODDS_CANVAS_RELIABLE_FALLBACK",
        f"One-frame read missed goal №{next_goal_number}; using reliable multi-frame OCR",
    )
    odds = await hybrid_market._read_next_goal_odds_canvas(
        page,
        team1,
        team2,
        next_goal_number,
        logger,
    )
    return odds, None


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
    """Read odds quickly and inspect visual lock state.

    The normal Canvas path uses one screenshot for OCR and lock detection. A lock
    never hides recognized coefficients from the frontend. The DEMO runtime
    decides whether the selected side may be virtually placed.
    """
    odds, lock_state = await _read_odds_canvas_first(
        page,
        team1,
        team2,
        score1,
        score2,
        logger,
        read_only=read_only,
    )

    if odds.source != "CANVAS_VISION":
        return odds

    team1_locator = odds.team1_locator
    team2_locator = odds.team2_locator
    if not isinstance(team1_locator, hybrid_market.CanvasCoefficientLocator) or not isinstance(
        team2_locator, hybrid_market.CanvasCoefficientLocator
    ):
        return odds

    # The fast path has already inspected the exact same pixels used for OCR.
    # Only the reliable fallback needs a second screenshot for lock metadata.
    if lock_state is None:
        lock_state = await detect_locked_next_goal_sides(
            page,
            team1_region=team1_locator.region,
            team2_region=team2_locator.region,
            canvas_width=team1_locator.canvas_width,
            canvas_height=team1_locator.canvas_height,
        )

    locked_sides = tuple(int(side) for side in lock_state["locked_sides"])
    if locked_sides:
        await _log(
            logger,
            "CANVAS_LOCK_STATE",
            (
                f"Следующий гол №{odds.next_goal_number}: "
                f"locked_sides={list(locked_sides)} markers={lock_state['markers']}"
            ),
        )

    if lock_state["error"]:
        await _log(
            logger,
            "CANVAS_LOCK_DETECTOR_SKIPPED",
            lock_state["error"],
        )

    return replace(
        odds,
        locked_sides=locked_sides,
        lock_markers=lock_state["markers"],
    )
