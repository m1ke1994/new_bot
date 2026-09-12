import unittest
from unittest.mock import AsyncMock, patch

from backend.app.table_tennis.models import TableTennisMatch, TableTennisObservation
from backend.app.table_tennis.monitoring import (
    find_target_market_group,
    is_candidate_set,
    map_outcomes_to_players,
    parse_party_number,
    read_second_set_1x2_odds,
    read_target_market_odds,
    select_candidate_matches,
    target_market_title,
    target_set_for,
)


def make_match(current_set):
    return TableTennisMatch(
        event_id=f"event-{current_set}",
        league_id="league-1",
        league_name="League",
        player_1="Player A",
        player_2="Player B",
        score="0:0",
        sets_score=None,
        current_set=current_set,
        points_player_1=None,
        points_player_2=None,
        status="LIVE",
        started=True,
        time=None,
        href=f"/match-{current_set}",
        url=f"https://example.test/match-{current_set}",
    )


class TableTennisMonitoringLogicTests(unittest.TestCase):
    def test_candidate_sets_only_first_and_second_party(self):
        self.assertTrue(is_candidate_set(1))
        self.assertTrue(is_candidate_set(2))
        self.assertFalse(is_candidate_set(3))
        self.assertFalse(is_candidate_set(4))
        self.assertFalse(is_candidate_set(None))

        selected = select_candidate_matches([
            make_match(1),
            make_match(2),
            make_match(3),
            make_match(4),
        ])
        self.assertEqual([item.current_set for item in selected], [1, 2])

    def test_target_party_is_next_party(self):
        self.assertEqual(target_set_for(1), 2)
        self.assertEqual(target_set_for(2), 3)
        self.assertIsNone(target_set_for(3))
        self.assertIsNone(target_set_for(None))

    def test_party_text_parser_is_case_insensitive(self):
        self.assertEqual(parse_party_number("1-я Партия"), 1)
        self.assertEqual(parse_party_number("2-я партия"), 2)
        self.assertEqual(parse_party_number("3-я ПАРТИЯ"), 3)
        self.assertEqual(parse_party_number("2 сет"), 2)
        self.assertIsNone(parse_party_number("Основная игра"))

    def test_outcomes_map_p1_to_left_player_and_p2_to_right_player(self):
        mapped = map_outcomes_to_players("Left", "Right", 1.87, 2.05)
        self.assertEqual(mapped["p1"], {"player": "Left", "odds": 1.87})
        self.assertEqual(mapped["p2"], {"player": "Right", "odds": 2.05})

    def test_initial_odds_are_saved_once_and_current_odds_keep_updating(self):
        observation = TableTennisObservation.from_match(
            make_match(2),
            current_set=2,
            target_set=3,
        )

        first = observation.record_odds(1.87, 1.87, "2026-09-10T12:30:00+00:00")
        unchanged = observation.record_odds(1.87, 1.87, "2026-09-10T12:30:01+00:00")
        changed = observation.record_odds(1.65, 2.15, "2026-09-10T12:30:02+00:00")

        self.assertTrue(first["first"])
        self.assertFalse(unchanged["changed"])
        self.assertTrue(changed["changed"])
        self.assertEqual(observation.initial_odds_p1, 1.87)
        self.assertEqual(observation.initial_odds_p2, 1.87)
        self.assertEqual(observation.current_odds_p1, 1.65)
        self.assertEqual(observation.current_odds_p2, 2.15)
        self.assertEqual(len(observation.odds_history), 2)

    def test_market_lock_does_not_erase_initial_odds(self):
        observation = TableTennisObservation.from_match(
            make_match(1),
            current_set=1,
            target_set=2,
        )
        observation.record_odds(1.72, 2.10, "2026-09-10T12:30:00+00:00")
        observation.mark_market_locked("2026-09-10T12:30:01+00:00")

        self.assertEqual(observation.market_status, "MARKET_LOCKED")
        self.assertEqual(observation.initial_odds_p1, 1.72)
        self.assertEqual(observation.initial_odds_p2, 2.10)
        self.assertIsNone(observation.current_odds_p1)
        self.assertIsNone(observation.current_odds_p2)

    def test_target_market_title_is_exact_party_market(self):
        self.assertEqual(target_market_title(2), "1X2. 2-я Партия")
        self.assertEqual(target_market_title(3), "1X2. 3-я Партия")


class _FakeTextLocator:
    def __init__(self, text=None):
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        return 0 if self.text is None else 1

    async def inner_text(self):
        return self.text or ""


class _FakeMarketGroup:
    def __init__(self, title):
        self.title = title

    def locator(self, _selector):
        return _FakeTextLocator(self.title)


class _FakeGroupCollection:
    def __init__(self, groups):
        self.groups = groups

    async def count(self):
        return len(self.groups)

    def nth(self, index):
        return self.groups[index]


class _FakeMarketPage:
    def __init__(self, titles):
        self.groups = [_FakeMarketGroup(title) for title in titles]

    def locator(self, _selector):
        return _FakeGroupCollection(self.groups)


class TableTennisMarketLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_market_group_is_rejected_and_exact_target_is_selected(self):
        wrong_only = _FakeMarketPage(["1X2. 2-я Партия", "Тотал. 3-я Партия"])
        self.assertIsNone(await find_target_market_group(wrong_only, 3))

        page = _FakeMarketPage(["1X2. 2-я Партия", "1X2. 3-я Партия"])
        selected = await find_target_market_group(page, 3)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.title, "1X2. 3-я Партия")


class _MarketCollection:
    def __init__(self, nodes):
        self.nodes = nodes

    @property
    def first(self):
        return self.nodes[0] if self.nodes else _DomNode()

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class _DomNode:
    def __init__(self, *, text="", title="", outcomes=None, opened=True):
        self.text = text
        self.title = title
        self.outcomes = outcomes or []
        self.opened = opened
        self.clicked = False

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.text or self.title or self.outcomes else 0

    async def inner_text(self):
        return self.text or self.title

    async def get_attribute(self, _name):
        return ""

    async def is_disabled(self):
        return False

    async def is_visible(self):
        return self.opened

    async def click(self, **_kwargs):
        self.clicked = True
        self.opened = True

    async def wait_for(self, **_kwargs):
        return None

    def locator(self, selector):
        if "game-markets-group-header-title" in selector:
            return _DomNode(text=self.title)
        if selector == ".game-markets-group__header":
            header = _DomNode(text="header", opened=True)

            async def open_market(**_kwargs):
                header.clicked = True
                self.opened = True

            header.click = open_market
            self.header = header
            return header
        if selector == ".game-markets-group__market":
            buttons = []
            for name, value in self.outcomes:
                button = _DomNode(text="button", opened=self.opened)
                button.locator = lambda child, n=name, v=value: _DomNode(
                    text=n if child == ".ui-market__name" else v
                )
                buttons.append(button)
            return _MarketCollection(buttons)
        return _DomNode()


class _ExactMarketPage:
    url = "https://example.test/event/42"

    def __init__(self):
        self.total = _DomNode(
            title="Тотал. 2-я Партия",
            outcomes=[("П1", "9.99"), ("П2", "8.88")],
        )
        self.target = _DomNode(
            title="1X2. 2-я Партия",
            outcomes=[("П1", "1.65"), ("П2", "2.15")],
            opened=False,
        )

    def locator(self, _selector):
        return _MarketCollection([self.total, self.target])


class ExactSecondSetOddsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_only_exact_second_set_1x2_and_opens_accordion(self):
        page = _ExactMarketPage()
        with (
            patch("backend.app.table_tennis.monitoring.ensure_same_event"),
            patch(
                "backend.app.table_tennis.monitoring.read_selected_party",
                new=AsyncMock(return_value=2),
            ),
        ):
            market = await read_target_market_odds(page, "42", 2)

        self.assertIsNotNone(market)
        self.assertTrue(market.available)
        self.assertEqual((market.p1, market.p2), (1.65, 2.15))
        self.assertTrue(page.target.header.clicked)

    async def test_second_set_reader_returns_frontend_shape(self):
        page = _ExactMarketPage()
        with (
            patch("backend.app.table_tennis.monitoring.ensure_same_event"),
            patch(
                "backend.app.table_tennis.monitoring.read_selected_party",
                new=AsyncMock(return_value=2),
            ),
        ):
            result = await read_second_set_1x2_odds(page, "42")

        self.assertEqual(result, {
            "player1_odd": 1.65,
            "player2_odd": 2.15,
            "market": "1X2. 2-я Партия",
            "available": True,
        })


if __name__ == "__main__":
    unittest.main()
