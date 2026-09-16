import unittest

from backend.app.browser.scoreboard import (
    SCOREBOARD_ROOT_SELECTOR,
    SCOREBOARD_ROOT_SELECTORS,
    TEAM_1_SCORE_SELECTOR,
    TEAM_1_SCORE_SELECTORS,
    TEAM_2_SCORE_SELECTOR,
    TEAM_2_SCORE_SELECTORS,
    TEAM_SELECTOR as SCOREBOARD_TEAM_SELECTOR,
    TEAM_SELECTORS as SCOREBOARD_TEAM_SELECTORS,
    TIMER_SELECTOR as SCOREBOARD_TIMER_SELECTOR,
    TIMER_SELECTORS as SCOREBOARD_TIMER_SELECTORS,
    parse_timer_and_period,
)
from backend.app.browser.market import (
    MARKET_BUTTON_SELECTOR,
    MARKET_GROUP_SELECTOR,
    MARKET_GROUP_TITLE_SELECTOR,
    MARKET_NAME_SELECTOR,
    MARKET_VALUE_SELECTOR,
    NEXT_GOAL_SEARCH_SELECTOR,
    TOTAL_EVEN_SEARCH_TEXT,
    TOTAL_EVEN_SELECTION_TEXT,
    TOTAL_EVEN_TEXT,
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
        self.assertEqual(MATCH_LINK_SELECTOR, "a.dashboard-game-block__link")
        self.assertEqual(TEAM_SELECTOR, ".dashboard-game-team-info__name")
        self.assertEqual(TIME_SELECTOR, ".dashboard-game-info__time")
        self.assertEqual(PERIOD_SELECTOR, ".dashboard-game-info__period")
        self.assertEqual(
            SCORE_SELECTOR,
            ".ui-game-scores__item--total .ui-game-scores__num",
        )

    def test_match_links_are_limited_to_configured_league(self):
        self.assertTrue(
            is_league_match_href(
                "/ru/live/fifa/2860561-fc-25-3x3-conference-league/749-test"
            )
        )
        self.assertFalse(
            is_league_match_href("/ru/live/fifa/another-league/749-test")
        )

    def test_scoreboard_selectors_support_both_confirmed_layouts(self):
        self.assertIn(".scoreboard-layout-head__footer", SCOREBOARD_ROOT_SELECTORS)
        self.assertIn(
            ".scoreboard-compact-view-tab-panel__body",
            SCOREBOARD_ROOT_SELECTORS,
        )
        self.assertIn(".scoreboard-intro__team", SCOREBOARD_TEAM_SELECTORS)
        self.assertIn(".scoreboard-team-name__text", SCOREBOARD_TEAM_SELECTORS)
        self.assertIn(
            ".scoreboard-scores__item--team-1",
            TEAM_1_SCORE_SELECTORS,
        )
        self.assertIn(
            ".scoreboard-scores__score:not(.scoreboard-scores__score--team-2)",
            TEAM_1_SCORE_SELECTORS,
        )
        self.assertIn(
            ".scoreboard-scores__item--team-2",
            TEAM_2_SCORE_SELECTORS,
        )
        self.assertIn(
            ".scoreboard-scores__score--team-2",
            TEAM_2_SCORE_SELECTORS,
        )
        self.assertIn(".scoreboard-timer span", SCOREBOARD_TIMER_SELECTORS)
        self.assertIn(".ui-game-timer__label", SCOREBOARD_TIMER_SELECTORS)

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

    def test_total_even_dom_labels_are_explicit(self):
        self.assertEqual(TOTAL_EVEN_SEARCH_TEXT, "тотал чет")
        self.assertEqual(TOTAL_EVEN_TEXT, "Тотал чёт")
        self.assertEqual(TOTAL_EVEN_SELECTION_TEXT, "Да")


if __name__ == "__main__":
    unittest.main()
