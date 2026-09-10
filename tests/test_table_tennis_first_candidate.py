import unittest
from unittest.mock import AsyncMock, patch

from backend.app.table_tennis.first_candidate_scanner import (
    FirstCandidateTableTennisScanner,
    first_monitorable_candidate,
)
from backend.app.table_tennis.models import TableTennisLeague, TableTennisMatch
from backend.app.table_tennis.state import TableTennisStateStore


class FakeNode:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None


class FakePage:
    def __init__(self):
        self.url = "about:blank"

    def locator(self, _selector):
        return FakeNode()

    async def goto(self, url, **_kwargs):
        self.url = url


class FakeManager:
    def __init__(self):
        self.page = FakePage()

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}


class FirstCandidateTests(unittest.IsolatedAsyncioTestCase):
    def make_match(self, event_id, current_set, *, url=True):
        return TableTennisMatch(
            event_id=event_id,
            league_id="1",
            league_name="League",
            player_1=f"P1-{event_id}",
            player_2=f"P2-{event_id}",
            score=None,
            sets_score=None,
            current_set=current_set,
            points_player_1=None,
            points_player_2=None,
            status="LIVE",
            started=True,
            time=None,
            href=f"/event/{event_id}" if url else None,
            url=f"https://example.test/event/{event_id}" if url else None,
            is_candidate=current_set in (1, 2),
        )

    def test_first_monitorable_candidate_keeps_dom_order(self):
        third_set = self.make_match("10", 3)
        missing_url = self.make_match("11", 1, url=False)
        first = self.make_match("12", 2)
        second = self.make_match("13", 1)

        selected = first_monitorable_candidate([third_set, missing_url, first, second])

        self.assertIs(selected, first)

    def test_first_monitorable_candidate_respects_excluded_ids(self):
        first = self.make_match("12", 2)
        second = self.make_match("13", 1)

        selected = first_monitorable_candidate([first, second], {"12"})

        self.assertIs(selected, second)

    async def test_background_search_stops_after_first_league_with_candidate(self):
        state = TableTennisStateStore()
        manager = FakeManager()
        scanner = FirstCandidateTableTennisScanner(manager, state)
        leagues = [
            TableTennisLeague("1", "First", "/1", "https://example.test/1"),
            TableTennisLeague("2", "Second", "/2", "https://example.test/2"),
        ]
        candidate = self.make_match("101", 1)
        later = self.make_match("202", 2)
        scanner._scan_league_until_candidate = AsyncMock(
            side_effect=[([candidate], candidate), ([later], later)]
        )

        with patch(
            "backend.app.table_tennis.first_candidate_scanner.scan_leagues",
            AsyncMock(return_value=leagues),
        ):
            result = await scanner._find_first_candidate(
                manager.page,
                "https://example.test/ru/live/table-tennis",
            )

        self.assertIs(result, candidate)
        self.assertEqual(scanner._scan_league_until_candidate.await_count, 1)
        snapshot = await state.snapshot()
        self.assertEqual(snapshot["status"], "CANDIDATE_FOUND")
        self.assertIsNone(snapshot["current_league"])
        self.assertEqual(snapshot["candidates_count"], 1)


if __name__ == "__main__":
    unittest.main()
