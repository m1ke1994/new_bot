import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser import canvas_locking_adapter as adapter
from backend.app.demo.models import NextGoalOdds


class CanvasLockingCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_lock_from_canvas_2d_snapshot_without_screenshot(self):
        snapshot = {
            "snapshot_at_ms": 1200,
            "texts": [
                {
                    "seq": 10,
                    "kind": "fillText",
                    "event_type": "text",
                    "text": "\ue91d",
                    "x": 110,
                    "y": 50,
                    "width": 12,
                    "height": 16,
                    "alpha": 1.0,
                }
            ],
            "images": [],
            "paths": [],
            "rects": [],
            "events": [],
        }
        with patch.object(
            adapter,
            "capture_canvas_2d_snapshot",
            new=AsyncMock(return_value=snapshot),
        ):
            result = await adapter.detect_locked_next_goal_sides(
                object(),
                team1_region={"x": 80, "y": 40, "width": 90, "height": 36},
                team2_region={"x": 220, "y": 40, "width": 90, "height": 36},
                canvas_width=400,
                canvas_height=200,
            )

        self.assertEqual(result["locked_sides"], [1])
        self.assertEqual(result["source"], "CANVAS_2D")
        self.assertIsNone(result["error"])

    async def test_reader_delegates_to_canvas_2d_only(self):
        odds = NextGoalOdds(
            team1=2.14,
            team2=1.71,
            market="Следующий гол №4",
            next_goal_number=4,
            source="CANVAS_2D",
            ocr_backend="fillText",
            confidence=1.0,
        )
        reader = AsyncMock(return_value=odds)
        with patch.object(adapter, "read_next_goal_odds_canvas_2d", new=reader):
            result = await adapter.read_next_goal_odds(
                object(),
                "Roma",
                "Nice",
                2,
                1,
                read_only=True,
            )

        self.assertIs(result, odds)
        reader.assert_awaited_once()
        self.assertEqual(reader.await_args.kwargs["read_only"], True)


if __name__ == "__main__":
    unittest.main()
