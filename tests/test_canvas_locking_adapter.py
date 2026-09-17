import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from backend.app.browser import canvas_locking_adapter as adapter
from backend.app.browser import market as hybrid_market
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
        marker = adapter.detect_lock_marker(
            _locked_row_image(),
            {"x": 92, "y": 43, "width": 42, "height": 15},
            side=1,
            canvas_width=300,
            canvas_height=100,
        )

        self.assertIsNotNone(marker)
        self.assertGreaterEqual(marker["lower_fill"], 0.68)
        self.assertGreaterEqual(marker["bottom_fill"], 0.74)
        self.assertLessEqual(marker["distance_to_odds"], 80)

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


class CanvasLockingHybridTests(unittest.IsolatedAsyncioTestCase):
    async def test_canvas_visual_lock_keeps_odds_and_adds_metadata(self):
        page = object()
        canvas_shape = {"width": 800, "height": 400}
        team1_locator = hybrid_market.CanvasCoefficientLocator(
            page,
            {"x": 100, "y": 80, "width": 80, "height": 30},
            canvas_shape,
        )
        team2_locator = hybrid_market.CanvasCoefficientLocator(
            page,
            {"x": 420, "y": 80, "width": 80, "height": 30},
            canvas_shape,
        )
        odds = NextGoalOdds(
            team1=2.14,
            team2=1.71,
            market="Следующий гол №4",
            next_goal_number=4,
            source="CANVAS_VISION",
            ocr_backend="RapidOCR",
            confidence=0.94,
            team1_locator=team1_locator,
            team2_locator=team2_locator,
        )

        with patch.object(
            adapter.hybrid_market,
            "read_next_goal_odds",
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
            result = await adapter.read_next_goal_odds(
                page,
                "Roma",
                "Nice",
                2,
                1,
            )

        self.assertEqual(result.team1, 2.14)
        self.assertEqual(result.team2, 1.71)
        self.assertEqual(result.locked_sides, (1,))
        self.assertIn("1", result.lock_markers)

    async def test_dom_result_is_not_rechecked_as_canvas(self):
        page = object()
        odds = NextGoalOdds(
            team1=2.0,
            team2=1.8,
            market="Следующий гол №1",
            next_goal_number=1,
            source="DOM_PLAYWRIGHT",
            ocr_backend=None,
            confidence=None,
            team1_locator=object(),
            team2_locator=object(),
        )
        detector = AsyncMock()

        with patch.object(
            adapter.hybrid_market,
            "read_next_goal_odds",
            new=AsyncMock(return_value=odds),
        ), patch.object(
            adapter,
            "detect_locked_next_goal_sides",
            new=detector,
        ):
            result = await adapter.read_next_goal_odds(
                page,
                "Roma",
                "Nice",
                0,
                0,
            )

        self.assertIs(result, odds)
        detector.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
