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

    def test_long_series_checkbox_and_runtime_status_exist(self):
        self.assertIn("long_series_enabled", self.source)
        self.assertIn("LONG_SERIES", self.source)
        self.assertIn("state.long_series?.state", self.source)
        self.assertIn("Следующий матч", self.source)
        self.assertIn("Наблюдение", self.source)
        self.assertIn("active_observation", self.source)
        self.assertIn("observations", self.source)
        self.assertIn("Нет активной ставки · LONG_SERIES наблюдает матчи", self.source)
        self.assertIn("Последние наблюдения", self.source)

    def test_team1_profile_filter_controls_and_observation_exist(self):
        self.assertIn("team1_profile_enabled", self.source)
        self.assertIn("team1_profile_max_odds", self.source)
        self.assertIn("TEAM_1 + КФ ниже", self.source)
        self.assertIn("TEAM_1 PROFILE SHADOW", self.source)
        self.assertIn("team1ProfileObservationHistory", self.source)
        self.assertIn("TEAM1 PROFILE наблюдает матч", self.source)

    def test_runtime_limit_controls_offer_only_supported_durations(self):
        self.assertIn("run_time_limit_enabled", self.source)
        self.assertIn("run_duration_hours", self.source)
        self.assertIn("Время работы бота", self.source)
        self.assertIn('v-for="hours in [3, 6, 12, 24]"', self.source)
        self.assertIn("runTimeRemaining", self.source)
        self.assertIn("уже ACCEPTED ставки", self.source)

    def test_removed_tactics_are_not_exposed(self):
        router = ROUTER.read_text(encoding="utf-8")
        self.assertNotIn("/forks", router)
        self.assertNotIn("TOTAL_EVEN", self.source)
        self.assertNotIn("Тотал чёт", self.source)
        self.assertNotIn("Настольный теннис", self.source)


if __name__ == "__main__":
    unittest.main()
