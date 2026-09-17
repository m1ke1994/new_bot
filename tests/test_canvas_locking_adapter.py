import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from backend.app.browser import canvas_locking_adapter as adapter
from backend.app.browser.canvas_market_adapter import CanvasCoefficientLocator
from backend.app.demo.models import NextGoalOdds


def _locked_row_image() -> np.ndarray:
    image = np.full((100, 300, 3), 238, dtype=np.uint8)
    lock = np.array(
        [
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0],
            [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
            [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
        ],
        dtype=np.uint8,
    )
    roi = image[44:56, 28:39]
    roi[lock == 1] = (110, 110, 110)
    return image


class CanvasLockMarkerTests(unittest.TestCase):
    def test_detects_padlock_before_team_outcome(self):
        image = _locked_row_image()
        marker = adapter.detect_lock_marker(
            image,
            {"x": 92, "y": 43, "width": 42, "height": 15},
            side=1,
            canvas_width=300,
            canvas_height=100,
        )

        self.assertIsNotNone(marker)
        self.assertGreaterEqual(marker["lower_fill"], 0.64)
        self.assertGreaterEqual(marker["bottom_fill"], 0.70)

    def test_plain_outcome_has_no_lock(self):
        image = np.full((100, 300, 3), 238, dtype=np.uint8)
        marker = adapter.detect_lock_marker(
            image,
            {"x": 92, "y": 43, "width": 42, "height": 15},
            side=1,
            canvas_width=300,
            canvas_height=100,
        )

        self.assertIsNone(marker)


class CanvasLockingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_visual_lock_is_exposed_as_market_locked(self):
        page = object()
        team1_locator = CanvasCoefficientLocator(
            page,
            outcome_box={"x": 100, "y": 80, "width": 80, "height": 30},
            canvas_width=800,
            canvas_height=400,
        )
        team2_locator = CanvasCoefficientLocator(
            page,
            outcome_box={"x": 420, "y": 80, "width": 80, "height": 30},
            canvas_width=800,
            canvas_height=400,
        )
        odds = NextGoalOdds(
            team1=2.14,
            team2=1.71,
            market="Следующий гол (4)",
            next_goal_number=4,
            source="CANVAS_OCR",
            ocr_backend="RapidOCR",
            confidence=0.94,
            team1_locator=team1_locator,
            team2_locator=team2_locator,
        )

        with patch.object(
            adapter,
            "_read_canvas_aware_next_goal_odds",
            new=AsyncMock(return_value=odds),
        ), patch.object(
            adapter,
            "detect_locked_next_goal_sides",
            new=AsyncMock(
                return_value={
                    "locked_sides": [1],
                    "markers": {"1": {"x": 28, "y": 44, "width": 11, "height": 12}},
                    "error": None,
                }
            ),
        ):
            with self.assertRaises(adapter.dom_market.MarketNotAvailable) as raised:
                await adapter.read_next_goal_odds(
                    page,
                    "Roma",
                    "Nice",
                    2,
                    1,
                )

        self.assertEqual(raised.exception.status, "MARKET_LOCKED")
        self.assertEqual(raised.exception.details["locked_sides"], [1])
        self.assertTrue(raised.exception.details["market_available"])
        self.assertEqual(
            raised.exception.details["source"],
            "CANVAS_VISUAL_LOCK",
        )


if __name__ == "__main__":
    unittest.main()
