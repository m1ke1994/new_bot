import unittest
from pathlib import Path


class FrontendBetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.app_source = (root / "front/frontend/src/App.vue").read_text(
            encoding="utf-8"
        )
        cls.engine_source = (root / "backend/app/demo/engine.py").read_text(
            encoding="utf-8"
        )

    def test_current_bet_panel_reads_backend_bet_contract(self):
        required_bindings = (
            "show(bet.amount)",
            "show(bet.market",
            "show(bet.odds)",
            "show(bet.score_before)",
            "show(bet.next_goal_number)",
        )
        for binding in required_bindings:
            with self.subTest(binding=binding):
                self.assertIn(binding, self.app_source)

    def test_team_odds_remain_visible_before_and_after_team_selection(self):
        self.assertIn("state.odds?.team1", self.app_source)
        self.assertIn("state.odds?.team2", self.app_source)
        self.assertIn(
            "state.odds?.team1 != null || state.odds?.team2 != null",
            self.app_source,
        )

    def test_frontend_exposes_actual_hybrid_reader_metadata(self):
        self.assertIn(
            "state.value.odds?.source || marketReader.value.source || null",
            self.app_source,
        )
        self.assertIn("state.value.odds?.backend", self.app_source)
        self.assertIn("state.value.odds?.confidence", self.app_source)
        self.assertIn("source.includes('CANVAS')", self.app_source)
        self.assertIn("машинным зрением по Canvas", self.app_source)
        self.assertNotIn(
            "Рынки читаются напрямую через Playwright DOM.",
            self.app_source,
        )

    def test_engine_publishes_current_bet_odds_and_goal_number(self):
        self.assertIn('"odds": selected_odd', self.engine_source)
        self.assertIn('"score_before": score_before.text()', self.engine_source)
        self.assertIn(
            '"next_goal_number": current_odds.next_goal_number',
            self.engine_source,
        )
        self.assertIn(
            "odds=self._odds_state(current_odds, selected_odd, opponent_odd)",
            self.engine_source,
        )


if __name__ == "__main__":
    unittest.main()
