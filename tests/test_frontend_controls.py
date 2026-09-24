import unittest
from pathlib import Path

APP_VUE = Path(__file__).parents[1] / "front" / "frontend" / "src" / "App.vue"
ROUTER = APP_VUE.parent / "router" / "index.js"


class FrontendControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_VUE.read_text(encoding="utf-8")

    def test_next_goal_blocked_events_checkbox_and_history_label_exist(self):
        self.assertIn("blocked_events_switch_enabled", self.source)
        self.assertIn("Заблокированные события", self.source)
        self.assertIn("item.result === 'MISSED_SELECTED_TEAM_GOAL'", self.source)

    def test_max_three_checkbox_explicitly_applies_to_demo_and_live(self):
        self.assertIn("max_three_steps_enabled", self.source)
        self.assertIn("DEMO и LIVE", self.source)
        self.assertIn("NOT_PLACED шаг не расходуют", self.source)

    def test_removed_tactics_are_not_exposed(self):
        router = ROUTER.read_text(encoding="utf-8")
        self.assertNotIn("/forks", router)
        self.assertNotIn("TOTAL_EVEN", self.source)
        self.assertNotIn("Тотал чёт", self.source)
        self.assertNotIn("Настольный теннис", self.source)


if __name__ == "__main__":
    unittest.main()
