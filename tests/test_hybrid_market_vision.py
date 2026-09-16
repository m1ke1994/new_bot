import unittest

from backend.app.browser.market import (
    CanvasCoefficientLocator,
    parse_next_goal_market_name,
)


class HybridMarketVisionTests(unittest.TestCase):
    def test_next_goal_label_parser_supports_team_rows(self):
        self.assertEqual(parse_next_goal_market_name("Команда 1 - 3-й гол"), (1, 3))
        self.assertEqual(parse_next_goal_market_name("Команда 2 — 3-й гол"), (2, 3))
        self.assertIsNone(parse_next_goal_market_name("Не будет 3-го гола"))

    def test_canvas_click_position_scales_screenshot_pixels_to_css_pixels(self):
        locator = CanvasCoefficientLocator(
            page=None,
            region={"x": 400, "y": 200, "width": 100, "height": 40},
            canvas_shape={"width": 1000, "height": 500},
        )
        position = locator._position({"width": 500, "height": 250})
        self.assertAlmostEqual(position["x"], 225.0)
        self.assertAlmostEqual(position["y"], 110.0)

    def test_canvas_click_position_is_clamped_inside_canvas(self):
        locator = CanvasCoefficientLocator(
            page=None,
            region={"x": 995, "y": 495, "width": 50, "height": 50},
            canvas_shape={"width": 1000, "height": 500},
        )
        position = locator._position({"width": 500, "height": 250})
        self.assertLess(position["x"], 500)
        self.assertLess(position["y"], 250)
        self.assertGreater(position["x"], 0)
        self.assertGreater(position["y"], 0)


if __name__ == "__main__":
    unittest.main()
