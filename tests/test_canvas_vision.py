import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from backend.app.browser.canvas_vision import (
    CANVAS_SELECTOR,
    RapidOCR,
    CanvasVision,
    parse_odds,
)


class TextRecOutput:
    """RapidOCR recognition-only result: deliberately has no boxes."""

    def __init__(self, text="1.91", score=0.99):
        self.txts = (text,)
        self.scores = (score,)


class _FakeRapidEngine:
    def __init__(self, *, full_output, cached_output=None):
        self.full_output = full_output
        self.cached_output = cached_output or TextRecOutput()
        self.calls = []

    def __call__(self, _image, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("use_det") is False:
            return self.cached_output
        return self.full_output


class CanvasVisionTests(unittest.TestCase):

    def test_canvas_selector_targets_market_grid_canvas(self):
        self.assertEqual(CANVAS_SELECTOR, "canvas.market-grid-canvas__canvas")

    def test_full_canvas_ocr_explicitly_restores_detection_mode(self):
        output = SimpleNamespace(
            txts=("1.91", "2.05"),
            scores=(0.99, 0.98),
            boxes=np.array(
                [
                    [[10, 20], [90, 20], [90, 50], [10, 50]],
                    [[120, 20], [200, 20], [200, 50], [120, 50]],
                ],
                dtype=np.float32,
            ),
        )
        engine = _FakeRapidEngine(full_output=output)
        vision = CanvasVision()
        vision._initialized = True
        vision.rapidocr = engine

        regions = vision._rapid_regions(
            np.zeros((100, 240, 3), dtype=np.uint8)
        )

        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["text"], "1.91")
        self.assertEqual(regions[1]["text"], "2.05")
        self.assertEqual(
            engine.calls[-1],
            {"use_det": True, "use_cls": True, "use_rec": True},
        )

    def test_text_rec_output_without_boxes_does_not_crash_worker(self):
        engine = _FakeRapidEngine(full_output=TextRecOutput())
        vision = CanvasVision()
        vision._initialized = True
        vision.rapidocr = engine

        regions = vision._rapid_regions(
            np.zeros((100, 240, 3), dtype=np.uint8)
        )

        self.assertEqual(regions, [])
        self.assertIn("TextRecOutput", vision.rapidocr_error)
        self.assertIn("expected RapidOCROutput", vision.rapidocr_error)

    def test_cached_recognition_cannot_leave_full_ocr_without_detection(self):
        output = SimpleNamespace(
            txts=("1.91",),
            scores=(0.99,),
            boxes=np.array(
                [[[10, 20], [90, 20], [90, 50], [10, 50]]],
                dtype=np.float32,
            ),
        )
        engine = _FakeRapidEngine(
            full_output=output,
            cached_output=TextRecOutput("1.91", 0.99),
        )
        vision = CanvasVision()
        vision._initialized = True
        vision.rapidocr = engine
        image = np.zeros((100, 240, 3), dtype=np.uint8)

        cached = vision._recognize_cached_box(
            image,
            {"x": 10, "y": 20, "width": 80, "height": 30},
        )
        regions = vision._rapid_regions(image)

        self.assertIsNotNone(cached)
        self.assertEqual(len(regions), 1)
        self.assertEqual(engine.calls[0]["use_det"], False)
        self.assertEqual(engine.calls[1]["use_det"], True)

    @unittest.skipIf(RapidOCR is None, "RapidOCR package is not installed")
    def test_installed_rapidocr_detects_positioned_odds(self):
        image = np.full((220, 640, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "1.91",
            (30, 85),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.2,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            image,
            "2.05",
            (330, 175),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.2,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )

        vision = CanvasVision()
        regions = vision._detect(image)
        odds = [
            value
            for region in regions
            for value in parse_odds(region["text"])
        ]

        self.assertIn(1.91, odds)
        self.assertIn(2.05, odds)
        self.assertTrue(all(region["width"] > 0 for region in regions))
        self.assertIsNone(vision.rapidocr_error)

    def test_parse_odds(self):
        self.assertEqual(parse_odds("1.52 1,87 2.145 1.00"), [1.52, 1.87, 2.145])

    def test_saved_real_canvas_mapping_when_fixture_exists(self):
        fixtures = sorted(
            Path("backend/diagnostics/canvas").glob("canvas_original_*.png")
        )
        suitable = next(
            (path for path in fixtures if path.stat().st_size > 100_000),
            None,
        )
        if suitable is None:
            self.skipTest("Сохранённый diagnostic canvas отсутствует")
        image = cv2.imread(str(suitable))
        analysis = CanvasVision().analyze_image(image, save=False)
        self.assertEqual(analysis["status"], "CANVAS_ANALYZED")
        mapping = analysis["next_goal_mapping"]
        self.assertEqual(mapping["mapping_method"], "STRUCTURAL_OUTCOME_LABELS")
        self.assertGreater(mapping["team1"]["value"], 1.0)
        self.assertGreater(mapping["team2"]["value"], 1.0)


if __name__ == "__main__":
    unittest.main()
