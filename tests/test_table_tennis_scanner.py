import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.table_tennis.models import TableTennisLeague, TableTennisMatch
from backend.app.table_tennis.scanner import (
    ScanAlreadyRunning,
    TableTennisScanner,
    deduplicate_matches,
    parse_event_id,
    parse_league_id,
    parse_league_label,
    parse_match_card,
    scan_leagues,
)
from backend.app.table_tennis.selectors import (
    ACCORDION_TRIGGER_SELECTOR,
    LEAGUE_GAMES_COUNT_SELECTOR,
    LEAGUE_GROUP_SELECTOR,
    LEAGUE_LINK_SELECTOR,
    LEAGUE_TITLE_SELECTOR,
    MARKET_SELECTOR,
    MATCH_LINK_SELECTOR,
    PERIOD_SELECTOR,
    PLAYER_SELECTOR,
    SCORE_SELECTOR,
    TIME_SELECTOR,
)
from backend.app.table_tennis.state import TableTennisStateStore


class FakeCollection:
    def __init__(self, items=()):
        self.items = list(items)

    @property
    def first(self):
        return self.items[0] if self.items else FakeNode(present=False)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeNode:
    def __init__(self, text="", *, attrs=None, children=None, ancestor=None, present=True):
        self.text = text
        self.attrs = attrs or {}
        self.children = children or {}
        self.ancestor = ancestor
        self.present = present

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.present else 0

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def wait_for(self, **_kwargs):
        return None

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self):
        return None

    def locator(self, selector):
        if selector.startswith("xpath=ancestor"):
            return FakeCollection([self.ancestor] if self.ancestor else [])
        return FakeCollection(self.children.get(selector, []))


class FakePage:
    def __init__(self, *, groups=(), links=()):
        self.groups = list(groups)
        self.links = list(links)
        self.url = "about:blank"

    def locator(self, selector):
        if selector == LEAGUE_GROUP_SELECTOR:
            return FakeCollection(self.groups)
        if selector == LEAGUE_LINK_SELECTOR:
            return FakeCollection(self.links)
        return FakeCollection([FakeNode()])

    async def goto(self, url, **_kwargs):
        self.url = url


def league_link(league_id, name, count, *, group=None):
    href = f"/ru/live/table-tennis/{league_id}-slug"
    return FakeNode(
        f"{name} ({count})",
        attrs={"href": href},
        children={
            LEAGUE_TITLE_SELECTOR: [FakeNode(name)],
            LEAGUE_GAMES_COUNT_SELECTOR: [FakeNode(f"({count})")],
        },
        ancestor=group,
    )


class TableTennisParsingTests(unittest.TestCase):
    def test_league_url_parsing_rejects_event_url(self):
        self.assertEqual(
            parse_league_id("/ru/live/table-tennis/2685665-masters-russia"),
            "2685665",
        )
        self.assertIsNone(
            parse_league_id(
                "/ru/live/table-tennis/2685665-masters-russia/998877-player-a-player-b"
            )
        )
        self.assertEqual(
            parse_event_id(
                "/ru/live/table-tennis/2685665-masters-russia/998877-player-a-player-b"
            ),
            "998877",
        )

    def test_league_name_and_declared_count_are_separated(self):
        self.assertEqual(parse_league_label("Мастерс. Россия (9)"), ("Мастерс. Россия", 9))

    def test_duplicate_event_id_is_kept_once(self):
        first = {"event_id": "42", "player_1": "A"}
        second = {"event_id": "42", "player_1": "Changed duplicate"}
        self.assertEqual(deduplicate_matches([first, second]), [first])


class TableTennisDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_league_keeps_group_name(self):
        trigger = FakeNode("Мастерс", attrs={"aria-expanded": "true"})
        group = FakeNode(children={ACCORDION_TRIGGER_SELECTOR: [trigger]})
        nested = league_link("2685665", "Мастерс. Россия", 9, group=group)
        group.children[LEAGUE_LINK_SELECTOR] = [nested]
        standalone = league_link("2112017", "Мастерс. Суперлига", 5)
        page = FakePage(groups=[group], links=[standalone, nested])

        leagues = await scan_leagues(page, "https://example.test/ru/live/table-tennis", AsyncMock())

        by_id = {item.league_id: item for item in leagues}
        self.assertIsNone(by_id["2112017"].group_name)
        self.assertEqual(by_id["2685665"].group_name, "Мастерс")
        self.assertEqual(by_id["2685665"].declared_games_count, 9)

    async def test_missing_score_odds_period_and_time_do_not_break_match_parser(self):
        league = TableTennisLeague("1", "League", "/league", "https://example.test/league")
        link = FakeNode(attrs={"href": "/ru/live/table-tennis/1-league/77-a-b"})
        card = FakeNode(
            children={
                PLAYER_SELECTOR: [FakeNode("Player A"), FakeNode("Player B")],
                MATCH_LINK_SELECTOR: [link],
                SCORE_SELECTOR: [],
                PERIOD_SELECTOR: [],
                TIME_SELECTOR: [],
                MARKET_SELECTOR: [],
            }
        )

        result = await parse_match_card(card, league, "https://example.test")

        self.assertEqual(result.event_id, "77")
        self.assertIsNone(result.score)
        self.assertIsNone(result.time)
        self.assertEqual(result.odds, {})
        self.assertEqual(result.markets, {})


class FakeManager:
    def __init__(self):
        self.page = FakePage()
        self.stop_calls = 0

    async def ensure_page(self):
        return self.page

    async def snapshot(self):
        status = "CLOSED" if self.stop_calls else "OPEN"
        return {"status": status, "context": status, "page": status}

    async def stop(self):
        self.stop_calls += 1
        return await self.snapshot()


class TableTennisScannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_scan_is_rejected(self):
        scanner = TableTennisScanner(FakeManager(), TableTennisStateStore())
        async with scanner._scan_lock:
            with self.assertRaises(ScanAlreadyRunning):
                await scanner.scan()

    async def test_start_runs_in_background_and_stop_closes_playwright(self):
        manager = FakeManager()
        state = TableTennisStateStore()
        scanner = TableTennisScanner(manager, state)
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        scanner.scan = worker
        result = await scanner.start()
        await started.wait()

        self.assertTrue(result["scanning"])
        self.assertIsNotNone(scanner.task)
        stopped = await scanner.stop()

        self.assertEqual(manager.stop_calls, 1)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertFalse(stopped["scanning"])
        self.assertEqual(stopped["browser"]["status"], "CLOSED")

    async def test_one_league_error_does_not_discard_other_leagues(self):
        state = TableTennisStateStore()
        scanner = TableTennisScanner(FakeManager(), state)
        leagues = [
            TableTennisLeague("1", "Broken", "/1", "https://example.test/1"),
            TableTennisLeague("2", "Working", "/2", "https://example.test/2"),
        ]
        match = TableTennisMatch(
            event_id="22", league_id="2", league_name="Working",
            player_1="A", player_2="B", score=None, sets_score=None,
            current_set=None, points_player_1=None, points_player_2=None,
            status="LIVE", started=True, time=None, href=None, url=None,
        )
        with (
            patch("backend.app.table_tennis.scanner.TABLE_TENNIS_URL", "https://example.test/ru/live/table-tennis"),
            patch("backend.app.table_tennis.scanner.authorize", AsyncMock(return_value={"ok": True, "status": "AUTHORIZED"})),
            patch("backend.app.table_tennis.scanner.scan_leagues", AsyncMock(return_value=leagues)),
            patch("backend.app.table_tennis.scanner.scan_league_matches", AsyncMock(side_effect=[RuntimeError("timeout"), [match]])),
        ):
            result = await scanner.scan()

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["matches_found"], 1)
        self.assertEqual(result["league_errors"][0]["league"], "Broken")
        self.assertEqual(len(await state.leagues()), 2)
        self.assertEqual((await state.matches())[0]["event_id"], "22")


if __name__ == "__main__":
    unittest.main()
