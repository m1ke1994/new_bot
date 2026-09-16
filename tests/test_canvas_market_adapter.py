import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser import canvas_market_adapter as adapter


class FakeCanvas:
    def __init__(self):
        self.first = self
        self.click_calls = []

    async def wait_for(self, **kwargs):
        return None

    async def bounding_box(self):
        return {"x": 0, "y": 0, "width": 400, "height": 200}

    async def click(self, **kwargs):
        self.click_calls.append(kwargs)


class FakePage:
    def __init__(self):
        self.canvas = FakeCanvas()

    def locator(self, selector):
        return self.canvas


class CanvasMarketAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_canvas_click_scales_ocr_coordinates_to_css_size(self):
        page = FakePage()
        locator = adapter.CanvasCoefficientLocator(
            page,
            outcome_box={"x": 200, "y": 100, "width": 100, "height": 50},
            canvas_width=800,
            canvas_height=400,
        )

        await locator.click(timeout=2500)

        self.assertEqual(len(page.canvas.click_calls), 1)
        call = page.canvas.click_calls[0]
        self.assertEqual(call["timeout"], 2500)
        self.assertEqual(call["position"], {"x": 125.0, "y": 62.5})

    async def test_canvas_reader_returns_next_goal_odds_contract(self):
        page = FakePage()
        payload = {
            "ok": True,
            "status": "ODDS_READY",
            "source": "CANVAS_OCR",
            "ocr_backend": "RapidOCR",
            "confidence": 0.94,
            "market": "Следующий гол (4)",
            "next_goal_number": 4,
            "team1": {"name": "Roma", "odds": 2.14},
            "team2": {"name": "Nice", "odds": 1.71},
            "analysis": {
                "canvas": {"width": 800, "height": 400},
                "next_goal_mapping": {
                    "team1": {"x": 100, "y": 80, "width": 80, "height": 30},
                    "team2": {"x": 420, "y": 80, "width": 80, "height": 30},
                },
            },
        }

        with patch.object(adapter, "_prepare_canvas", new=AsyncMock()), patch.object(
            adapter,
            "get_next_goal_odds",
            new=AsyncMock(return_value=payload),
        ):
            odds = await adapter._read_canvas_odds(
                page,
                "Roma",
                "Nice",
                2,
                1,
                logger=None,
            )

        self.assertEqual(odds.team1, 2.14)
        self.assertEqual(odds.team2, 1.71)
        self.assertEqual(odds.next_goal_number, 4)
        self.assertEqual(odds.source, "CANVAS_OCR")
        self.assertEqual(odds.ocr_backend, "RapidOCR")
        self.assertAlmostEqual(odds.confidence, 0.94)
        self.assertIsInstance(odds.team1_locator, adapter.CanvasCoefficientLocator)
        self.assertIsInstance(odds.team2_locator, adapter.CanvasCoefficientLocator)

    async def test_canvas_reader_rejects_stale_goal_number(self):
        page = FakePage()
        payload = {
            "ok": True,
            "status": "ODDS_READY",
            "next_goal_number": 5,
            "team1": {"name": "Roma", "odds": 2.14},
            "team2": {"name": "Nice", "odds": 1.71},
            "analysis": {
                "canvas": {"width": 800, "height": 400},
                "next_goal_mapping": {
                    "team1": {"x": 100, "y": 80, "width": 80, "height": 30},
                    "team2": {"x": 420, "y": 80, "width": 80, "height": 30},
                },
            },
        }

        with patch.object(adapter, "_prepare_canvas", new=AsyncMock()), patch.object(
            adapter,
            "get_next_goal_odds",
            new=AsyncMock(return_value=payload),
        ):
            with self.assertRaises(adapter.dom_market.MarketNotAvailable):
                await adapter._read_canvas_odds(
                    page,
                    "Roma",
                    "Nice",
                    2,
                    1,
                    logger=None,
                )


if __name__ == "__main__":
    unittest.main()
