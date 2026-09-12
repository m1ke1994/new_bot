import asyncio
import unittest
from copy import deepcopy

from backend.app.table_tennis.fork_math import zero_zero_candidate_payload
from backend.app.table_tennis.idempotent_sequential_forks_scanner import (
    IdempotentSequentialForksTableTennisScanner,
    open_target_party_exact,
    read_selected_party_exact,
)
from backend.app.table_tennis.models import TableTennisMatch


class RecordingState:
    def __init__(self):
        self.values = {}
        self.candidates = []

    async def update(self, **changes):
        self.values.update(deepcopy(changes))
        return deepcopy(self.values)

    async def snapshot(self):
        return deepcopy(self.values)

    async def update_candidate(self, candidate):
        self.candidates.append(deepcopy(candidate))

    async def set_active_match(self, candidate):
        self.values["active_match"] = deepcopy(candidate)


def make_match(event_id="42"):
    return TableTennisMatch(
        event_id=event_id,
        league_id="league",
        league_name="League",
        player_1="Player 1",
        player_2="Player 2",
        score="0:0",
        sets_score="0:0",
        current_set=1,
        points_player_1=0,
        points_player_2=0,
        status="LIVE",
        started=True,
        time=None,
        href=f"/event/{event_id}",
        url=f"https://example.test/event/{event_id}",
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

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, **_kwargs):
        await self.item.click()


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
        for item in self.page.items:
            item.selected = False
        self.selected = True

    async def dispatch_event(self, _name):
        await self.click()


class _PartyPage:
    url = "https://1xbet-start.cc/ru/live/table-tennis/123-league/42-player-a-player-b"

    def __init__(self):
        self.items = [
            _PartyItem(self, "Основная игра", selected=True),
            _PartyItem(self, "2-я Партия"),
            _PartyItem(self, "3-я Партия"),
        ]

    def locator(self, selector):
        if "game-sub-games__item--is-selected" in selector or "aria-selected" in selector:
            return _Collection([item for item in self.items if item.selected])
        if selector == ".game-sub-games__list .game-sub-games__item":
            return _Collection(self.items)
        return _Collection([])


class FirstLegIdempotenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_party_two_is_clicked_from_main_game_and_verified_selected(self):
        page = _PartyPage()
        self.assertIsNone(await read_selected_party_exact(page))

        await open_target_party_exact(
            page,
            "42",
            2,
            asyncio.Event(),
            timeout=1.0,
        )

        self.assertEqual(await read_selected_party_exact(page), 2)
        selected = [item.text for item in page.items if item.selected]
        self.assertEqual(selected, ["2-я Партия"])

    async def test_same_event_retry_does_not_reserve_initial_stake_twice(self):
        state = RecordingState()
        scanner = IdempotentSequentialForksTableTennisScanner(
            browser_manager=object(),
            state=state,
        )
        await scanner.configure(budget=10_000, initial_stake=1000)

        first_payload = zero_zero_candidate_payload(make_match("42"))
        await scanner._record_first_leg(first_payload, side="p1", odds=1.87)

        self.assertEqual(scanner._available_balance, 9000.0)
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)

        history = await scanner.forks()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "FIRST_LEG_OPEN")
        self.assertEqual(history[0]["first_leg"]["accepted_odd"], 1.87)
        self.assertIsNone(history[0]["second_leg"])
        self.assertEqual(history[0]["total_invested"], 1000.0)

        # Simulate a full observation retry: the strategy creates a fresh payload
        # for the same event, exactly the condition that previously double-reserved.
        retry_payload = zero_zero_candidate_payload(make_match("42"))
        await scanner._record_first_leg(retry_payload, side="p1", odds=1.80)

        self.assertEqual(scanner._available_balance, 9000.0)
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(state.values["available_balance"], 9000.0)
        self.assertEqual(state.values["reserved_balance"], 1000.0)
        self.assertEqual(retry_payload["first_leg"]["accepted_odd"], 1.87)
        self.assertEqual(retry_payload["first_leg"]["stake"], 1000.0)
        self.assertEqual(retry_payload["monitoring_status"], "WAITING_FOR_ARB")

        # Retry updates the same history row, never creates a duplicate first leg.
        history = await scanner.forks()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["sequence_id"], retry_payload["sequence_id"])
        self.assertEqual(history[0]["status"], "FIRST_LEG_OPEN")

    async def test_preview_amount_does_not_change_bank_before_second_leg(self):
        state = RecordingState()
        scanner = IdempotentSequentialForksTableTennisScanner(
            browser_manager=object(),
            state=state,
        )
        await scanner.configure(budget=10_000, initial_stake=1000)
        payload = zero_zero_candidate_payload(make_match("99"))

        await scanner._record_first_leg(payload, side="p1", odds=1.87)
        # The mathematical second-leg preview may be around 1000 at the initial
        # 1.87/1.87 line, but it is not a placed bet and must not be reserved.
        payload["required_second_stake"] = 1000.0

        self.assertIsNone(payload["second_leg"])
        self.assertEqual(scanner._reserved_balance, 1000.0)
        self.assertEqual(scanner._available_balance, 9000.0)
        history = await scanner.forks()
        self.assertEqual(history[0]["total_invested"], 1000.0)
        self.assertIsNone(history[0]["second_leg"])


if __name__ == "__main__":
    unittest.main()
