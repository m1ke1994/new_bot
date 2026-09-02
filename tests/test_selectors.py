import unittest

from backend.app.browser.scoreboard import (
    SCOREBOARD_ROOT_SELECTOR,
    TEAM_1_SCORE_SELECTOR,
    TEAM_2_SCORE_SELECTOR,
    TEAM_SELECTOR as SCOREBOARD_TEAM_SELECTOR,
    TIMER_SELECTOR as SCOREBOARD_TIMER_SELECTOR,
    parse_timer_and_period,
)
from backend.app.browser.market import (
    MARKET_BUTTON_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_VALUE_SELECTOR,
    NEXT_GOAL_SEARCH_SELECTOR,
    parse_dom_odds,
    parse_next_goal_market_name,
)
from matches import (
    MATCH_LINK_SELECTOR,
    PERIOD_SELECTOR,
    SCORE_SELECTOR,
    TEAM_SELECTOR,
    TIME_SELECTOR,
    is_league_match_href,
)


class SelectorTests(unittest.TestCase):
    def test_league_card_selectors_match_confirmed_dom(self):
        self.assertEqual(MATCH_LINK_SELECTOR, "a.ui-game-card__link")
        self.assertEqual(TEAM_SELECTOR, ".ui-game-card-scoreboard__name")
        self.assertEqual(TIME_SELECTOR, ".ui-game-card__data")
        self.assertEqual(PERIOD_SELECTOR, ".ui-game-card__period")
        self.assertEqual(SCORE_SELECTOR, ".ui-game-card-scoreboard__score")

    def test_match_links_are_limited_to_configured_league(self):
        self.assertTrue(
            is_league_match_href(
                "/ru/live/fifa/2860561-fc-25-3x3-conference-league/749-test"
            )
        )
        self.assertFalse(
            is_league_match_href("/ru/live/fifa/another-league/749-test")
        )

    def test_scoreboard_selectors_match_confirmed_dom(self):
        self.assertEqual(SCOREBOARD_ROOT_SELECTOR, ".scoreboard-layout-head__footer")
        self.assertEqual(SCOREBOARD_TIMER_SELECTOR, ".scoreboard-timer span")
        self.assertEqual(SCOREBOARD_TEAM_SELECTOR, ".scoreboard-intro__team")
        self.assertEqual(TEAM_1_SCORE_SELECTOR, ".scoreboard-scores__item--team-1")
        self.assertEqual(TEAM_2_SCORE_SELECTOR, ".scoreboard-scores__item--team-2")

    def test_live_timer_contains_period_and_upcoming_timer_does_not(self):
        self.assertEqual(
            parse_timer_and_period("1-й тайм, 01:51"),
            ("01:51", "1-й тайм"),
        )
        self.assertEqual(parse_timer_and_period("19:01"), ("19:01", ""))

    def test_no_vue_scope_attributes_are_used(self):
        selectors = (
            MATCH_LINK_SELECTOR,
            TEAM_SELECTOR,
            TIME_SELECTOR,
            PERIOD_SELECTOR,
            SCORE_SELECTOR,
            SCOREBOARD_ROOT_SELECTOR,
            SCOREBOARD_TIMER_SELECTOR,
            SCOREBOARD_TEAM_SELECTOR,
            TEAM_1_SCORE_SELECTOR,
            TEAM_2_SCORE_SELECTOR,
            NEXT_GOAL_SEARCH_SELECTOR,
            MARKET_GROUP_SELECTOR,
            MARKET_GROUP_TITLE_SELECTOR,
            MARKET_BUTTON_SELECTOR,
            MARKET_NAME_SELECTOR,
            MARKET_VALUE_SELECTOR,
        )
        self.assertFalse(any("data-v-" in selector for selector in selectors))

    def test_next_goal_dom_market_names_are_mapped_by_side_and_number(self):
        self.assertEqual(parse_next_goal_market_name("Команда 1 - 3-й гол"), (1, 3))
        self.assertEqual(parse_next_goal_market_name("Команда 2 — 7-й гол"), (2, 7))
        self.assertIsNone(parse_next_goal_market_name("Не будет 3-го гола"))
        self.assertEqual(parse_dom_odds(" 1,984 "), 1.984)


if __name__ == "__main__":
    unittest.main()
