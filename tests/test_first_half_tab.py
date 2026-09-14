import unittest

from backend.app.browser.first_half import (
    FIRST_HALF_LABEL,
    SUBGAME_ITEM_SELECTOR,
    SUBGAME_LIST_SELECTOR,
    SUBGAME_SELECTED_CLASS,
    open_first_half,
    selected_subgame_text,
)


class FakeCaption:
    def __init__(self, text):
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def inner_text(self):
        return self.text


class FakeItem:
    def __init__(self, page, text, *, selected=False):
        self.page = page
        self.text = text
        self.classes = SUBGAME_ITEM_SELECTOR.removeprefix(".")
        if selected:
            self.classes += f" {SUBGAME_SELECTED_CLASS}"
        self.clicks = 0

    def locator(self, selector):
        if selector == ".ui-caption":
            return FakeCaption(self.text)
        raise AssertionError(f"Unexpected item selector: {selector}")

    async def inner_text(self):
        return self.text

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        return self.classes if name == "class" else None

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.clicks += 1
        for item in self.page.items:
            item.classes = SUBGAME_ITEM_SELECTOR.removeprefix(".")
        self.classes += f" {SUBGAME_SELECTED_CLASS}"


class FakeItems:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeRoot:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None


class FakePage:
    def __init__(self):
        self.items = [
            FakeItem(self, "Основная игра", selected=True),
            FakeItem(self, FIRST_HALF_LABEL),
            FakeItem(self, "2-й тайм"),
        ]

    def locator(self, selector):
        if selector == SUBGAME_LIST_SELECTOR:
            return FakeRoot()
        if selector == f"{SUBGAME_LIST_SELECTOR} {SUBGAME_ITEM_SELECTOR}":
            return FakeItems(self.items)
        raise AssertionError(f"Unexpected page selector: {selector}")


class FirstHalfTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_exact_first_half_li_and_verifies_selected_class(self):
        page = FakePage()

        result = await open_first_half(page)

        self.assertEqual(result, FIRST_HALF_LABEL)
        self.assertEqual(page.items[0].clicks, 0)
        self.assertEqual(page.items[1].clicks, 1)
        self.assertEqual(page.items[2].clicks, 0)
        self.assertEqual(await selected_subgame_text(page), FIRST_HALF_LABEL)
        self.assertNotIn(SUBGAME_SELECTED_CLASS, page.items[0].classes.split())
        self.assertIn(SUBGAME_SELECTED_CLASS, page.items[1].classes.split())

    async def test_does_not_click_when_first_half_is_already_selected(self):
        page = FakePage()
        page.items[0].classes = SUBGAME_ITEM_SELECTOR.removeprefix(".")
        page.items[1].classes += f" {SUBGAME_SELECTED_CLASS}"

        result = await open_first_half(page)

        self.assertEqual(result, FIRST_HALF_LABEL)
        self.assertEqual(page.items[1].clicks, 0)


if __name__ == "__main__":
    unittest.main()
