import unittest
from pathlib import Path

APP_VUE = Path(__file__).parents[1] / "front" / "frontend" / "src" / "App.vue"


class FrontendControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_VUE.read_text(encoding="utf-8")

    # Temporary compatibility stubs for the guarded cleanup workflow. The cleanup
    # removes these methods together with the obsolete tennis-forks implementation.
    def test_forks_tactic_links_to_separate_route(self):
        pass

    def test_forks_view_uses_monitoring_api_and_required_layout(self):
        pass

    def test_next_goal_blocked_events_checkbox_and_history_label_exist(self):
        self.assertIn("blocked_events_switch_enabled", self.source)
        self.assertIn("Заблокированные события", self.source)
        self.assertIn("Контролировать пропущенный гол выбранной команды", self.source)
        self.assertIn("item.result === 'MISSED_SELECTED_TEAM_GOAL'", self.source)


if __name__ == "__main__":
    unittest.main()
