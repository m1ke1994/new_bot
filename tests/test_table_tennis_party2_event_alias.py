import asyncio
import time
import unittest

from backend.app.table_tennis import stable_forks_scanner as stable_module
from backend.app.table_tennis.party2_alias_forks_scanner import (
    _match_identity,
    ensure_pinned_party_session,
    event_url_matches_party_session,
    read_selected_party_with_alias,
    reset_party_two_alias_state,
)


ROOT_URL = (
    "https://1xbet-start.cc/ru/live/table-tennis/2685665-masters-russia/"
    "752178208-pavel-mukhin-igor-egorov-v"
)
PARTY_TWO_URL = (
    "https://1xbet-start.cc/ru/live/table-tennis/2685665-masters-russia/"
    "752178210-pavel-mukhin-igor-egorov-v"
)
OTHER_MATCH_URL = (
    "https://1xbet-start.cc/ru/live/table-tennis/2685665-masters-russia/"
    "752178999-other-player-another-player"
)


class _Collection:
    def __init__(self, items):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0] if self.items else _EmptyNode()

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _EmptyNode:
    @property
    def first(self):
        return self

    async def count(self):
        return 0


class _Caption:
    def __init__(self, item):
        self.item = item

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def inner_text(self):
        return self.item.text


class _PartyItem:
    def __init__(self, page, text, selected=False):
        self.page = page
        self.text = text
        self.selected = selected

    def locator(self, selector):
        if selector == ".ui-caption":
            return _Caption(self)
        return _EmptyNode()

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, **_kwargs):
        if self.text == "2-я Партия":
            self.page.party_two_clicks += 1
            # Production regression: 1xBet changes the numeric event id and the
            # refreshed DOM may visually mark «Основная игра» again. Do not model
            # Party 2 as selected here; the alias id itself must stop re-clicking.
            self.page.url = PARTY_TWO_URL


class _ReloadingPartyPage:
    def __init__(self):
        self.url = ROOT_URL
        self.party_two_clicks = 0
        self.items = [
            _PartyItem(self, "Основная игра", selected=True),
            _PartyItem(self, "2-я Партия", selected=False),
            _PartyItem(self, "3-я Партия", selected=False),
        ]

    def locator(self, selector):
        if selector == stable_module._PARTY_ITEM_SELECTOR:
            return _Collection(self.items)
        if selector == stable_module._SELECTED_PARTY_SELECTOR:
            return _Collection([item for item in self.items if item.selected])
        return _Collection([])


class _SimplePage:
    def __init__(self, url):
        self.url = url


class PartyTwoEventAliasRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_party_two_alias_state()
        stable_module._party_click_at.clear()

    def test_identity_keeps_same_match_when_only_event_id_changes(self):
        root_event, root_key = _match_identity(ROOT_URL)
        party_event, party_key = _match_identity(PARTY_TWO_URL)
        self.assertEqual(root_event, "752178208")
        self.assertEqual(party_event, "752178210")
        self.assertEqual(root_key, party_key)

    def test_party_two_event_id_is_accepted_after_party_click(self):
        self.assertTrue(event_url_matches_party_session(ROOT_URL, "752178208"))
        stable_module._party_click_at["752178208"] = time.monotonic()

        self.assertTrue(
            event_url_matches_party_session(PARTY_TWO_URL, "752178208")
        )
        # Once registered, the alias remains valid for the series without another
        # click and therefore cannot trigger a reload back to the root event.
        stable_module._party_click_at["752178208"] = 0.0
        self.assertTrue(
            event_url_matches_party_session(PARTY_TWO_URL, "752178208")
        )

    def test_unrelated_match_is_still_rejected(self):
        self.assertTrue(event_url_matches_party_session(ROOT_URL, "752178208"))
        stable_module._party_click_at["752178208"] = time.monotonic()
        self.assertFalse(
            event_url_matches_party_session(OTHER_MATCH_URL, "752178208")
        )

    async def test_one_party_two_click_survives_reload_to_main_visual_state(self):
        page = _ReloadingPartyPage()

        await stable_module.open_party_two_stable(
            page,
            "752178208",
            2,
            asyncio.Event(),
            timeout=2.0,
        )

        self.assertEqual(page.party_two_clicks, 1)
        self.assertEqual(page.url, PARTY_TWO_URL)
        self.assertEqual(await read_selected_party_with_alias(page), 2)
        ensure_pinned_party_session(page, "752178208")

    async def test_party_alias_does_not_depend_on_selected_css_after_reload(self):
        self.assertTrue(event_url_matches_party_session(ROOT_URL, "752178208"))
        stable_module._party_click_at["752178208"] = time.monotonic()
        self.assertTrue(
            event_url_matches_party_session(PARTY_TWO_URL, "752178208")
        )
        page = _SimplePage(PARTY_TWO_URL)

        # No DOM at all: the registered sub-event is still semantically Party 2.
        page.locator = lambda _selector: _Collection([])
        self.assertEqual(await read_selected_party_with_alias(page), 2)


if __name__ == "__main__":
    unittest.main()
