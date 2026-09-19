from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Page

from .canvas_2d_adapter import (
    capture_canvas_2d_snapshot,
    detect_lock_state,
    read_next_goal_odds as read_next_goal_odds_canvas_2d,
)


Logger = Callable[[str, str], Awaitable[Any]]


async def detect_locked_next_goal_sides(
    page: Page,
    *,
    team1_region: dict[str, Any],
    team2_region: dict[str, Any],
    canvas_width: float,
    canvas_height: float,
) -> dict[str, Any]:
    """Compatibility lock detector backed only by Canvas 2D draw calls.

    The size arguments remain for old callers. No screenshot, OCR, cv2 or numpy
    path is used.
    """
    del canvas_width, canvas_height
    try:
        snapshot = await capture_canvas_2d_snapshot(page)
        result = detect_lock_state(
            snapshot,
            team1_region={
                "x": float(team1_region["x"]),
                "y": float(team1_region["y"]),
                "width": float(team1_region["width"]),
                "height": float(team1_region["height"]),
            },
            team2_region={
                "x": float(team2_region["x"]),
                "y": float(team2_region["y"]),
                "width": float(team2_region["width"]),
                "height": float(team2_region["height"]),
            },
        )
        return {
            "locked_sides": list(result["locked_sides"]),
            "recent_locked_sides": list(result.get("recent_locked_sides") or ()),
            "markers": result["markers"],
            "recent_markers": result.get("recent_markers") or {},
            "error": None,
            "source": "CANVAS_2D",
        }
    except Exception as error:
        return {
            "locked_sides": [],
            "recent_locked_sides": [],
            "markers": {},
            "recent_markers": {},
            "error": f"{type(error).__name__}: {error}",
            "source": "CANVAS_2D",
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
    """Compatibility wrapper: NEXT_GOAL is Canvas 2D/DOM only, never CV/OCR."""
    return await read_next_goal_odds_canvas_2d(
        page,
        team1,
        team2,
        score1,
        score2,
        logger,
        read_only=read_only,
    )
