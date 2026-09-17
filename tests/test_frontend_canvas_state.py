import unittest
from pathlib import Path


class FrontendCanvasStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "front/frontend/src/App.vue").read_text(encoding="utf-8")

    def test_team_odds_are_rendered_from_backend_state(self):
        self.assertIn("show(state.odds?.team1)", self.source)
        self.assertIn("show(state.odds?.team2)", self.source)

    def test_current_bet_renders_backend_odds_and_context(self):
        self.assertIn("show(bet.odds)", self.source)
        self.assertIn("show(bet.amount)", self.source)
        self.assertIn("show(bet.score_before)", self.source)
        self.assertIn("show(bet.next_goal_number)", self.source)
        self.assertIn("show(state.selected_team)", self.source)

    def test_canvas_source_metadata_replaces_stale_dom_only_copy(self):
        self.assertIn("const marketSource = computed", self.source)
        self.assertIn("state.value.odds?.source", self.source)
        self.assertIn("const marketBackend = computed", self.source)
        self.assertIn("const marketConfidence = computed", self.source)
        self.assertIn("машинным зрением по Canvas", self.source)
        self.assertNotIn("Рынки читаются напрямую через Playwright DOM.", self.source)
        self.assertNotIn("market_reader: { source: 'DOM / Playwright'", self.source)


if __name__ == "__main__":
    unittest.main()
