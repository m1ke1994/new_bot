from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import cv2
import numpy as np
from playwright.async_api import Page

from xbet_config import SELECTORS

from . import market as dom_market
from .canvas_market_adapter import (
    CanvasCoefficientLocator,
    read_next_goal_odds as _read_canvas_aware_next_goal_odds,
)
from .canvas_vision import CANVAS_SELECTOR, decode_canvas_image


Logger = Callable[[str, str], Awaitable[Any]]


async def _log(logger: Logger | None, event: str, message: str) -> None:
    if logger is not None:
        await logger(event, message)


def _scaled_box(
    outcome_box: dict[str, Any],
    *,
    canvas_width: float,
    canvas_height: float,
    image_width: int,
    image_height: int,
) -> dict[str, float]:
    scale_x = image_width / canvas_width
    scale_y = image_height / canvas_height
    return {
        "x": float(outcome_box["x"]) * scale_x,
        "y": float(outcome_box["y"]) * scale_y,
        "width": float(outcome_box["width"]) * scale_x,
        "height": float(outcome_box["height"]) * scale_y,
    }


def detect_lock_marker(
    image: Any,
    outcome_box: dict[str, Any],
    *,
    side: int,
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any] | None:
    """Find the small padlock drawn immediately before a team next-goal outcome."""
    if side not in {1, 2}:
        raise ValueError(f"Unsupported side: {side}")
    if image is None or image.size == 0 or canvas_width <= 0 or canvas_height <= 0:
        return None

    image_height, image_width = image.shape[:2]
    box = _scaled_box(
        outcome_box,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        image_width=image_width,
        image_height=image_height,
    )

    center_y = box["y"] + box["height"] / 2
    half_band = max(9, min(20, round(box["height"] * 0.90)))
    top = max(0, round(center_y - half_band))
    bottom = min(image_height, round(center_y + half_band + 1))

    # The canvas layout maps team 1 to the left outcome column and team 2 to
    # the middle column. Search only the label area belonging to that side so
    # a lock on "Не будет N-го гола" (right column) cannot mark a team outcome
    # as blocked.
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

        # Padlock silhouette: a sparse shackle in the upper part and a dense,
        # almost solid body below it.
        if not (0.05 <= upper_fill <= 0.72):
            continue
        if lower_fill < 0.64 or bottom_fill < 0.70:
            continue
        if lower_fill - upper_fill < 0.08:
            continue

        center_x = left + x + width / 2
        # Favor the marker closest to the OCR-mapped odds, where the bookmaker
        # draws the lock icon.
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


async def detect_locked_next_goal_sides(
    page: Page,
    *,
    team1_box: dict[str, Any],
    team2_box: dict[str, Any],
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any]:
    """Capture the market canvas once and report visually locked team outcomes."""
    try:
        canvas_selector = SELECTORS.canvas or CANVAS_SELECTOR
        canvas = page.locator(canvas_selector).first
        await canvas.wait_for(state="visible", timeout=5_000)
        image_bytes = await canvas.screenshot(type="png")
        image = decode_canvas_image(image_bytes)

        markers: dict[str, Any] = {}
        for side, outcome_box in ((1, team1_box), (2, team2_box)):
            marker = detect_lock_marker(
                image,
                outcome_box,
                side=side,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
            )
            if marker is not None:
                markers[str(side)] = marker

        return {
            "locked_sides": sorted(int(side) for side in markers),
            "markers": markers,
            "error": None,
        }
    except Exception as error:
        # Detection is an additional safety trigger. If the capture itself
        # fails, preserve the previously working OCR/DOM behavior.
        return {
            "locked_sides": [],
            "markers": {},
            "error": f"{type(error).__name__}: {error}",
        }


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
    """Read odds using the existing adapter and add a canvas lock-state guard."""
    odds = await _read_canvas_aware_next_goal_odds(
        page,
        team1,
        team2,
        score1,
        score2,
        logger,
        read_only=read_only,
    )

    if odds.source != "CANVAS_OCR":
        return odds

    team1_locator = odds.team1_locator
    team2_locator = odds.team2_locator
    if not isinstance(team1_locator, CanvasCoefficientLocator) or not isinstance(
        team2_locator, CanvasCoefficientLocator
    ):
        return odds

    lock_state = await detect_locked_next_goal_sides(
        page,
        team1_box=team1_locator.outcome_box,
        team2_box=team2_locator.outcome_box,
        canvas_width=team1_locator.canvas_width,
        canvas_height=team1_locator.canvas_height,
    )

    locked_sides = lock_state["locked_sides"]
    if locked_sides:
        await _log(
            logger,
            "CANVAS_MARKET_LOCKED",
            (
                f"Следующий гол №{odds.next_goal_number}: "
                f"визуально заблокированы стороны {locked_sides}"
            ),
        )
        raise dom_market.MarketNotAvailable(
            (
                f"Рынок следующего гола №{odds.next_goal_number} "
                "заблокирован букмекером."
            ),
            status="MARKET_LOCKED",
            details={
                "source": "CANVAS_VISUAL_LOCK",
                "next_goal_number": odds.next_goal_number,
                "locked_sides": locked_sides,
                "market_available": True,
                "lock_markers": lock_state["markers"],
            },
        )

    if lock_state["error"]:
        await _log(
            logger,
            "CANVAS_LOCK_DETECTOR_SKIPPED",
            lock_state["error"],
        )

    return odds
