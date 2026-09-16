import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser.market import (
    NEXT_GOAL_SEARCH_TEXT,
    CanvasCoefficientLocator,
    _locate_market_search_input,
    _prepare_market_search,
    parse_next_goal_market_name,
    read_next_goal_odds,
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


class _FakeSearchElement:
    def __init__(self, kind, events, *, visible=True):
        self.kind = kind
        self.events = events
        self.visible = visible
        self.first = self
        self.value = ""

    async def count(self):
        return 1

    async def is_visible(self):
        return self.visible

    async def scroll_into_view_if_needed(self):
        self.events.append(f"{self.kind}:scroll")

    async def click(self):
        self.events.append(f"{self.kind}:click")

    async def fill(self, value):
        self.value = value
        self.events.append(f"{self.kind}:fill:{value}")

    async def input_value(self):
        return self.value


class _FakeSearchCollection:
    def __init__(self, items):
        self.items = items
        self.first = items[0] if items else _FakeSearchElement("empty", [], visible=False)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _TwoSearchInputsPage:
    def __init__(self):
        self.events = []
        self.upper_input = _FakeSearchElement("upper-input", self.events)
        self.market_input = _FakeSearchElement("market-input", self.events)

    def locator(self, selector):
        if selector.startswith(".game-panel__markets") or selector.startswith(
            ".market-grid-game-panel__markets"
        ):
            return _FakeSearchCollection([])
        if selector.startswith("input"):
            return _FakeSearchCollection([self.upper_input, self.market_input])
        return _FakeSearchCollection([])

    async def wait_for_timeout(self, timeout):
        self.events.append(f"page:wait:{timeout}")


class MarketSearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_selects_second_visible_input_from_top(self):
        page = _TwoSearchInputsPage()

        search_input, selector, visible_position = (
            await _locate_market_search_input(page)
        )

        self.assertIs(search_input, page.market_input)
        self.assertEqual(selector, "input.ui-search-default__input")
        self.assertEqual(visible_position, 1)

    async def test_clicks_paired_lower_button_before_filling_market_input(self):
        page = _TwoSearchInputsPage()
        button = _FakeSearchElement("market-button", page.events)

        with (
            patch(
                "backend.app.browser.market._locate_market_search_input",
                new_callable=AsyncMock,
                return_value=(
                    page.market_input,
                    "input.ui-search-default__input",
                    1,
                ),
            ),
            patch(
                "backend.app.browser.market._paired_market_search_button",
                new_callable=AsyncMock,
                return_value=(
                    button,
                    "paired:button.ui-search-default__button",
                ),
            ),
        ):
            selector = await _prepare_market_search(page, "следующий гол")

        self.assertEqual(selector, "input.ui-search-default__input")
        self.assertLess(
            page.events.index("market-button:click"),
            page.events.index("market-input:click"),
        )
        self.assertLess(
            page.events.index("market-input:click"),
            page.events.index("market-input:fill:следующий гол"),
        )
        self.assertEqual(page.upper_input.value, "")
        self.assertEqual(page.market_input.value, "следующий гол")


class NextGoalDemoSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_demo_still_opens_and_fills_market_search(self):
        page = object()
        expected_odds = object()

        with (
            patch(
                "backend.app.browser.market._prepare_market_search",
                new_callable=AsyncMock,
            ) as prepare_search,
            patch(
                "backend.app.browser.market._read_next_goal_odds_dom",
                new_callable=AsyncMock,
                return_value=expected_odds,
            ) as read_dom,
        ):
            result = await read_next_goal_odds(
                page,
                "Команда 1",
                "Команда 2",
                0,
                0,
                read_only=True,
            )

        self.assertIs(result, expected_odds)
        prepare_search.assert_awaited_once_with(
            page,
            NEXT_GOAL_SEARCH_TEXT,
            None,
        )
        read_dom.assert_awaited_once_with(
            page,
            "Команда 1",
            "Команда 2",
            1,
            None,
        )


if __name__ == "__main__":
    unittest.main()
