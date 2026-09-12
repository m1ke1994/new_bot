import unittest
from pathlib import Path

APP_VUE = Path(__file__).parents[1] / "front" / "frontend" / "src" / "App.vue"
FORKS_VIEW = APP_VUE.parent / "views" / "ForksView.vue"
ROUTER = APP_VUE.parent / "router" / "index.js"

class FrontendControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_VUE.read_text(encoding="utf-8")

    def test_forks_tactic_links_to_separate_route(self):
        self.assertIn('to="/forks"', self.source)
        self.assertIn("Вилки", self.source)
        self.assertIn("Настольный теннис", self.source)
        self.assertIn("path: '/forks'", ROUTER.read_text(encoding="utf-8"))

    def test_forks_view_uses_monitoring_api_and_required_layout(self):
        source = FORKS_VIEW.read_text(encoding="utf-8")
        self.assertIn("/api/table-tennis/candidates", source)
        self.assertIn("Вилки — мониторинг", source)
        self.assertIn("Начальный П1", source)
        self.assertIn("Текущий П1", source)
        self.assertIn("ДВИЖЕНИЕ СРЕДСТВ", source)
        self.assertIn("state.available_balance", source)
        self.assertIn("state.reserved_balance", source)
        self.assertIn("active.first_bet_wait_reason", source)
        self.assertIn("WAITING_ODDS_DIVERGENCE", source)
        self.assertIn("liveMatchesOpen", source)
        self.assertNotIn("<h2>Лиги</h2>", source)
        self.assertNotIn(">LEAGUES<", source)

if __name__ == "__main__":
    unittest.main()
