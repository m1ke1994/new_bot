import asyncio
import unittest

from backend.app.table_tennis import simple_party2_forks_scanner as module
from backend.app.table_tennis.monitoring import TargetPartyNotAvailable


class _Empty:
    @property
    def first(self):
        return self

    async def count(self):
        return 0


class _Caption:
    def __init__(self, text):
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def inner_text(self):
        return self.text


class _Item:
    def __init__(self, text):
        self.text = text
        self.clicks = 0

    def locator(self, selector):
        if selector == ".ui-caption":
            return _Caption(self.text)
        return _Empty()

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, **_kwargs):
        self.clicks += 1


class _Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0] if self.items else _Empty()

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _Page:
    def __init__(self, *, include_party_two=True):
        captions = ["Основная игра"]
        if include_party_two:
            captions.append("2-я Партия")
        captions.append("3-я Партия")
        self.items = [_Item(text) for text in captions]

    def locator(self, selector):
        if selector == module._PARTY_ITEM_SELECTOR:
            return _Collection(self.items)
        return _Collection([])


class SimplePartyTwoFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module._party_two_clicked_events.clear()
        module._active_event_id = "123"

    async def test_party_two_is_clicked_only_once_without_selected_or_url_checks(self):
        page = _Page()
        stop = asyncio.Event()

        await module.open_party_two_once(page, "123", 2, stop, timeout=0.1)
        await module.open_party_two_once(page, "123", 2, stop, timeout=0.1)

        self.assertEqual(page.items[1].clicks, 1)
        self.assertEqual(await module.read_selected_party_simple(page), 2)

    async def test_missing_party_two_expires_instead_of_waiting_forever(self):
        page = _Page(include_party_two=False)
        stop = asyncio.Event()

        with self.assertRaises(TargetPartyNotAvailable):
            await module.open_party_two_once(page, "123", 2, stop, timeout=0.05)

        self.assertNotIn("123", module._party_two_clicked_events)

    def test_market_titles_for_party_two_are_supported(self):
        self.assertEqual(module._market_title_kind("1X2"), 2)
        self.assertEqual(module._market_title_kind("1X2. 2-я Партия"), 1)
        self.assertEqual(module._market_title_kind("Тотал. 2-я Партия"), 0)

    def test_runtime_event_guard_is_disabled(self):
        self.assertIsNone(module._ignore_event_check(object(), "123"))
        self.assertTrue(module._current_match_is_open("anything", "123"))


if __name__ == "__main__":
    unittest.main()
