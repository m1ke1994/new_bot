import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser.market import (
    CanvasOutcomeLocator,
    _read_next_goal_odds_from_canvas,
)


class FakeCanvas:
    def __init__(self):
        self.first = self
        self.wait_calls = []
        self.click_calls = []

    async def wait_for(self, **kwargs):
        self.wait_calls.append(kwargs)

    async def bounding_box(self):
        return {"x": 10, "y": 20, "width": 800, "height": 400}

    async def click(self, **kwargs):
        self.click_calls.append(kwargs)


class FakePage:
    def __init__(self):
        self.canvas = FakeCanvas()
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return self.canvas


class CanvasOutcomeLocatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_uses_center_of_ocr_region(self):
        page = FakePage()
        locator = CanvasOutcomeLocator(
            page,
            {"x": 100, "y": 50, "width": 80, "height": 40},
            side=1,
            expected_odds=2.15,
        )

        await locator.click(timeout=3210)

        self.assertEqual(len(page.canvas.click_calls), 1)
        call = page.canvas.click_calls[0]
        self.assertEqual(call["timeout"], 3210)
        self.assertEqual(call["position"], {"x": 140.0, "y": 70.0})

    async def test_canvas_result_is_converted_to_next_goal_odds(self):
        page = FakePage()
        result = {
            "ok": True,
            "status": "ODDS_READY",
            "source": "CANVAS_OCR",
            "ocr_backend": "RapidOCR",
            "confidence": 0.96,
            "market": "Следующий гол (4)",
            "next_goal_number": 4,
            "team1": {"name": "Roma", "odds": 2.14},
            "team2": {"name": "Nice", "odds": 1.71},
            "analysis": {
                "next_goal_mapping": {
                    "team1": {"x": 120, "y": 80, "width": 70, "height": 35},
                    "team2": {"x": 420, "y": 80, "width": 70, "height": 35},
                }
            },
        }

        with patch(
            "backend.app.browser.market.get_next_goal_odds",
            new=AsyncMock(return_value=result),
        ):
            odds = await _read_next_goal_odds_from_canvas(
                page, "Roma", "Nice", logger=None
            )

        self.assertEqual(odds.team1, 2.14)
        self.assertEqual(odds.team2, 1.71)
        self.assertEqual(odds.next_goal_number, 4)
        self.assertEqual(odds.source, "CANVAS_OCR")
        self.assertEqual(odds.ocr_backend, "RapidOCR")
        self.assertAlmostEqual(odds.confidence, 0.96)
        self.assertIsInstance(odds.team1_locator, CanvasOutcomeLocator)
        self.assertIsInstance(odds.team2_locator, CanvasOutcomeLocator)


if __name__ == "__main__":
    unittest.main()
