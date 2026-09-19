import unittest
from unittest.mock import AsyncMock, patch

from backend.app.browser.league import LeagueBrowser
from matches import is_upcoming_match, sort_upcoming_matches


class MatchSelectionTests(unittest.TestCase):
    def test_period_rule(self):
        self.assertTrue(is_upcoming_match(period="", finished=False, time_seconds=42))
        self.assertFalse(
            is_upcoming_match(period="1-й тайм", finished=False, time_seconds=42)
        )
        self.assertFalse(
            is_upcoming_match(period="2-й тайм", finished=False, time_seconds=42)
        )

    def test_nearest_upcoming_is_first(self):
        matches = [
            {"id": "late", "is_upcoming": True, "time_seconds": 120},
            {"id": "live", "is_upcoming": False, "time_seconds": 10},
            {"id": "nearest", "is_upcoming": True, "time_seconds": 25},
        ]
        result = sort_upcoming_matches(matches)
        self.assertEqual([item["id"] for item in result], ["nearest", "late"])


class LeagueTeamFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_excludes_banned_teams_before_nearest_match_selection(self):
        class FakeLinks:
            async def count(self):
                return len(items)

            def nth(self, index):
                return index

        items = [
            {
                "id": "chelsea",
                "team1": " Chelsea ",
                "team2": "Lille",
                "finished": False,
                "is_upcoming": True,
                "period": "",
                "time_seconds": 10,
            },
            {
                "id": "allowed",
                "team1": "Arsenal",
                "team2": "Milan",
                "finished": False,
                "is_upcoming": True,
                "period": "",
                "time_seconds": 20,
            },
            {
                "id": "roma",
                "team1": "Nice",
                "team2": "РОМА",
                "finished": False,
                "is_upcoming": True,
                "period": "",
                "time_seconds": 30,
            },
        ]

        links = FakeLinks()
        write_front = AsyncMock()
        with (
            patch(
                "backend.app.browser.league.find_league_container",
                AsyncMock(return_value=object()),
            ),
            patch("backend.app.browser.league.league_match_links", return_value=links),
            patch("backend.app.browser.league.match_card_for_link", side_effect=lambda item: item),
            patch("backend.app.browser.league.parse_match", AsyncMock(side_effect=items)),
            patch("backend.app.browser.league.write_front", write_front),
        ):
            browser = LeagueBrowser(AsyncMock())
            result = await browser.scan()

        self.assertEqual([item["id"] for item in result], ["allowed"])
        self.assertEqual(
            [item["excluded_team"] for item in browser.skipped_excluded],
            ["Chelsea", "РОМА"],
        )
        self.assertEqual(browser.last_scan_stats["excluded"], 2)
        write_front.assert_awaited_once_with(result)

    async def test_transient_empty_countdown_is_retried_before_selection(self):
        class FakeLinks:
            async def count(self):
                return 1

            def nth(self, index):
                return index

        transient = {
            "id": "nearest",
            "team1": "Nice",
            "team2": "West Ham",
            "finished": False,
            "is_upcoming": False,
            "period": "",
            "time": "",
            "time_seconds": None,
        }
        resolved = {
            **transient,
            "is_upcoming": True,
            "time": "09:11",
            "time_seconds": 551,
        }
        page = AsyncMock()
        with (
            patch(
                "backend.app.browser.league.find_league_container",
                AsyncMock(return_value=object()),
            ),
            patch(
                "backend.app.browser.league.league_match_links",
                return_value=FakeLinks(),
            ),
            patch(
                "backend.app.browser.league.match_card_for_link",
                side_effect=lambda item: item,
            ),
            patch(
                "backend.app.browser.league.parse_match",
                AsyncMock(side_effect=[transient, resolved]),
            ),
            patch("backend.app.browser.league.write_front", AsyncMock()),
        ):
            browser = LeagueBrowser(page, exclude_teams_enabled=False)
            result = await browser.scan()

        self.assertEqual([item["id"] for item in result], ["nearest"])
        self.assertEqual(browser.last_scan_stats["unclassified"], 0)
        page.wait_for_timeout.assert_awaited_once()

    async def test_unclassified_nearest_card_blocks_later_match_selection(self):
        class FakeLinks:
            async def count(self):
                return 2

            def nth(self, index):
                return index

        unresolved = {
            "id": "nearest",
            "team1": "Nice",
            "team2": "West Ham",
            "finished": False,
            "is_upcoming": False,
            "period": "",
            "time": "",
            "time_seconds": None,
        }
        later = {
            "id": "later",
            "team1": "Chelsea",
            "team2": "Fiorentina",
            "finished": False,
            "is_upcoming": True,
            "period": "",
            "time": "18:58",
            "time_seconds": 1138,
        }
        write_front = AsyncMock()
        side_effect = [unresolved, unresolved, unresolved, unresolved, later]
        with (
            patch(
                "backend.app.browser.league.find_league_container",
                AsyncMock(return_value=object()),
            ),
            patch(
                "backend.app.browser.league.league_match_links",
                return_value=FakeLinks(),
            ),
            patch(
                "backend.app.browser.league.match_card_for_link",
                side_effect=lambda item: item,
            ),
            patch(
                "backend.app.browser.league.parse_match",
                AsyncMock(side_effect=side_effect),
            ),
            patch("backend.app.browser.league.write_front", write_front),
        ):
            browser = LeagueBrowser(AsyncMock(), exclude_teams_enabled=False)
            result = await browser.scan()

        self.assertEqual(result, [])
        self.assertEqual(browser.last_scan_stats["unclassified"], 1)
        self.assertEqual(browser.skipped_unclassified[0]["id"], "nearest")
        write_front.assert_awaited_once_with([later])

    async def test_disabled_filter_keeps_excluded_team_in_candidates(self):
        class FakeLinks:
            async def count(self):
                return 1

            def nth(self, index):
                return index

        chelsea = {
            "id": "chelsea",
            "team1": "Chelsea",
            "team2": "Lille",
            "finished": False,
            "is_upcoming": True,
            "period": "",
            "time_seconds": 10,
        }
        with (
            patch(
                "backend.app.browser.league.find_league_container",
                AsyncMock(return_value=object()),
            ),
            patch(
                "backend.app.browser.league.league_match_links",
                return_value=FakeLinks(),
            ),
            patch(
                "backend.app.browser.league.match_card_for_link",
                side_effect=lambda item: item,
            ),
            patch(
                "backend.app.browser.league.parse_match",
                AsyncMock(return_value=chelsea),
            ),
            patch("backend.app.browser.league.write_front", AsyncMock()),
        ):
            browser = LeagueBrowser(AsyncMock(), exclude_teams_enabled=False)
            result = await browser.scan()

        self.assertEqual([item["id"] for item in result], ["chelsea"])
        self.assertEqual(browser.skipped_excluded, [])
        self.assertEqual(browser.last_scan_stats["excluded"], 0)


if __name__ == "__main__":
    unittest.main()
