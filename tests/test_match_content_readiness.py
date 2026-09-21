import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser.league import (
    LeagueBrowser,
    MatchContentLoadTimeout,
)
from backend.app.browser.scoreboard import ScoreReadError
from backend.app.demo.models import Score, ScoreboardSnapshot


class FakeLocator:
    def __init__(self, visible: bool):
        self.visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.visible else 0

    async def is_visible(self):
        return self.visible

    async def bounding_box(self):
        if not self.visible:
            return None
        return {"x": 0, "y": 0, "width": 900, "height": 500}


class FakePage:
    def __init__(self, *, market_ready: bool):
        self.market_ready = market_ready
        self.url = "https://example.test/match"
        self.waits = []

    def locator(self, selector):
        visible = self.market_ready and (
            "market-grid-canvas__canvas" in selector
            or "game-markets-group" in selector
            or "Поиск по рынкам" in selector
        )
        return FakeLocator(visible)

    async def wait_for_timeout(self, timeout_ms):
        self.waits.append(timeout_ms)


class MatchContentReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_requires_scoreboard_and_market_surface(self):
        page = FakePage(market_ready=True)
        browser = LeagueBrowser(page)
        scoreboard = ScoreboardSnapshot(
            "TEAM 1",
            "TEAM 2",
            Score(0, 0),
            "",
            "",
        )

        with patch(
            "backend.app.browser.league.read_scoreboard",
            AsyncMock(return_value=scoreboard),
        ):
            state = await browser.wait_match_content_ready(timeout_ms=100)

        self.assertTrue(state["ready"])
        self.assertEqual(state["score"], "0:0")
        self.assertTrue(state["market_selector"])
        self.assertEqual(state["attempts"], 1)

    async def test_timeout_when_scoreboard_and_market_never_render(self):
        page = FakePage(market_ready=False)
        browser = LeagueBrowser(page)

        with patch(
            "backend.app.browser.league.read_scoreboard",
            AsyncMock(side_effect=ScoreReadError("scoreboard loading")),
        ):
            with self.assertRaises(MatchContentLoadTimeout) as caught:
                await browser.wait_match_content_ready(timeout_ms=0)

        details = caught.exception.details
        self.assertFalse(details["scoreboard_ready"])
        self.assertFalse(details["market_ready"])
        self.assertEqual(details["url"], page.url)
        self.assertEqual(details["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
