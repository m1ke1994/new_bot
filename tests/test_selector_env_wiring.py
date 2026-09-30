import re
import unittest
from pathlib import Path


class SelectorEnvWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.runtime_files = [
            cls.root / "auth.py",
            cls.root / "matches.py",
            *sorted((cls.root / "backend/app/browser").glob("*.py")),
            *sorted((cls.root / "backend/app/live").glob("*.py")),
        ]

    def test_runtime_has_no_literal_playwright_locator_selectors(self):
        pattern = re.compile(
            r"(?:locator|wait_for_selector|query_selector(?:_all)?)\(\s*[\"']"
        )
        offenders = []
        for path in self.runtime_files:
            source = path.read_text(encoding="utf-8")
            if pattern.search(source):
                offenders.append(str(path.relative_to(self.root)))
        self.assertEqual(offenders, [])

    def test_runtime_has_no_literal_js_query_selectors(self):
        pattern = re.compile(r"querySelector(?:All)?\(\s*[\"']")
        offenders = []
        for path in self.runtime_files:
            source = path.read_text(encoding="utf-8")
            if pattern.search(source):
                offenders.append(str(path.relative_to(self.root)))
        self.assertEqual(offenders, [])

    def test_runtime_selector_constants_are_not_hardcoded_css(self):
        pattern = re.compile(
            r"(?m)^\s*[A-Z0-9_]*SELECTOR[A-Z0-9_]*\s*=\s*[\"']"
        )
        offenders = []
        for path in self.runtime_files:
            source = path.read_text(encoding="utf-8")
            if pattern.search(source):
                offenders.append(str(path.relative_to(self.root)))
        self.assertEqual(offenders, [])

    def test_env_example_contains_runtime_selector_groups(self):
        env_source = (self.root / ".env.example").read_text(encoding="utf-8")
        required = (
            "SELECTOR_AUTH_MARKER_FALLBACKS=",
            "SELECTOR_MARKET_SEARCH_SCOPED_INPUTS=",
            "SELECTOR_MARKET_SEARCH_ANCESTOR_XPATH=",
            "SELECTOR_MATCH_CONTENT_MARKET_SURFACES=",
            "SELECTOR_SCOREBOARD_FALLBACKS=",
            "SELECTOR_SCOREBOARD_TEAM_FALLBACKS=",
            "SELECTOR_SCOREBOARD_TEAM_1_SCORE_FALLBACKS=",
            "SELECTOR_SCOREBOARD_TEAM_2_SCORE_FALLBACKS=",
            "SELECTOR_SCOREBOARD_TIMER_FALLBACKS=",
            "SELECTOR_MARKET_CANVAS_FALLBACK=",
            "SELECTOR_COUPON_BET=",
            "SELECTOR_EMPTY_COUPON=",
            "SELECTOR_BET_AMOUNT_INPUTS=",
            "SELECTOR_BET_SUBMIT_BUTTONS=",
            "SELECTOR_BET_BALANCE=",
            "SELECTOR_LOCKED_BETS=",
            "SELECTOR_LOCKED_BET_TEXTS=",
            "SELECTOR_SUCCESS_MODAL=",
            "SELECTOR_SUCCESS_MODAL_CLOSE_BUTTONS=",
        )
        for key in required:
            with self.subTest(key=key):
                self.assertIn(key, env_source)


if __name__ == "__main__":
    unittest.main()
