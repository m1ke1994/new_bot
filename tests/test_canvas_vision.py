import unittest
from pathlib import Path

import cv2

from backend.app.browser.canvas_vision import CanvasVision, parse_odds


class CanvasVisionTests(unittest.TestCase):
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
