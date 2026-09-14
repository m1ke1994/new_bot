import unittest

from backend.app.browser.first_half import (
    GAME_OVER_PANEL_SELECTOR,
    GAME_OVER_TITLE_SELECTOR,
    SCOREBOARD_TIMER_STATUS_SELECTOR,
    first_half_end_signal,
    game_over_flag,
)
from backend.app.demo.models import Score, ScoreboardSnapshot


class FakeLocatorList:
    def __init__(self, items):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0] if self.items else FakeNode(visible=False, count_value=0)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeNode:
    def __init__(self, text="", *, children=None, visible=True, count_value=1):
        self.text = text
        self.children = children or {}
        self.visible = visible
        self.count_value = count_value

    async def count(self):
        return self.count_value

    async def is_visible(self):
        return self.visible

    async def inner_text(self):
        return self.text

    def locator(self, selector):
        return FakeLocatorList(self.children.get(selector, []))


class FakePage:
    def __init__(self, *, game_over=False, title="Игра завершена.", timer=""):
        self.game_over = game_over
        self.title = title
        self.timer = timer

    def locator(self, selector):
        if selector == GAME_OVER_PANEL_SELECTOR:
            if not self.game_over:
                return FakeLocatorList([])
            return FakeLocatorList(
                [
                    FakeNode(
                        children={
                            GAME_OVER_TITLE_SELECTOR: [FakeNode(self.title)]
                        }
                    )
                ]
            )
        if selector == SCOREBOARD_TIMER_STATUS_SELECTOR:
            return FakeLocatorList([FakeNode(self.timer)]) if self.timer else FakeLocatorList([])
        return FakeLocatorList([])


class FirstHalfGameOverTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_over_message_is_terminal_trigger(self):
        page = FakePage(game_over=True)
        snapshot = ScoreboardSnapshot("A", "B", Score(3, 3), "02:45", "1-й тайм")

        game_over, text = await game_over_flag(page)
        finished, evidence = await first_half_end_signal(page, snapshot)

        self.assertTrue(game_over)
        self.assertEqual(text, "Игра завершена.")
        self.assertTrue(finished)
        self.assertEqual(evidence, "game-over=Игра завершена.")

    async def test_absent_game_over_panel_does_not_finish_by_itself(self):
        page = FakePage(game_over=False, timer="1-й тайм, 02:59")
        snapshot = ScoreboardSnapshot("A", "B", Score(3, 3), "02:59", "1-й тайм")

        game_over, _ = await game_over_flag(page)
        finished, _ = await first_half_end_signal(page, snapshot)

        self.assertFalse(game_over)
        self.assertFalse(finished)


if __name__ == "__main__":
    unittest.main()
