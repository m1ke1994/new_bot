import unittest

from backend.app.browser.market import (
    CanvasCoefficientLocator,
    _prepare_market_search,
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


class _FakeSearchLocator:
    def __init__(self, kind, events):
        self.kind = kind
        self.events = events
        self.first = self
        self.value = ""

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def wait_for(self, **_kwargs):
        self.events.append(f"{self.kind}:visible")

    async def scroll_into_view_if_needed(self):
        self.events.append(f"{self.kind}:scroll")

    async def click(self):
        self.events.append(f"{self.kind}:click")

    async def fill(self, value):
        self.value = value
        self.events.append(f"{self.kind}:fill:{value}")

    async def input_value(self):
        return self.value


class _FakeSearchPage:
    def __init__(self):
        self.events = []
        self.button = _FakeSearchLocator("button", self.events)
        self.market_input = _FakeSearchLocator("input", self.events)

    def locator(self, selector):
        if selector.startswith("button"):
            return self.button
        return self.market_input

    async def wait_for_timeout(self, timeout):
        self.events.append(f"page:wait:{timeout}")


class MarketSearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_search_button_before_filling_market_input(self):
        page = _FakeSearchPage()

        selector = await _prepare_market_search(page, "следующий гол")

        self.assertEqual(
            selector,
            'input.ui-search-default__input[placeholder="Поиск по рынкам"]',
        )
        self.assertLess(
            page.events.index("button:click"),
            page.events.index("input:click"),
        )
        self.assertLess(
            page.events.index("input:click"),
            page.events.index("input:fill:следующий гол"),
        )
        self.assertEqual(page.market_input.value, "следующий гол")


if __name__ == "__main__":
    unittest.main()
