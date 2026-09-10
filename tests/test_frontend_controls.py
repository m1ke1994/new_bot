import unittest
from pathlib import Path


APP_VUE = Path(__file__).parents[1] / "front" / "frontend" / "src" / "App.vue"
FORKS_VIEW = APP_VUE.parent / "views" / "ForksView.vue"
ROUTER = APP_VUE.parent / "router" / "index.js"


class FrontendControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_VUE.read_text(encoding="utf-8")

    def test_start_and_stop_are_driven_by_backend_state(self):
        self.assertIn("const canStart = computed(() => (", self.source)
        self.assertIn("&& !state.value.running", self.source)
        self.assertIn("const canStop = computed(() => (", self.source)
        self.assertIn("&& sessionNeedsStop.value", self.source)
        self.assertIn(':disabled="!canStart"', self.source)
        self.assertIn(':disabled="!canStop"', self.source)

    def test_backend_unavailable_and_clear_history_ux_exist(self):
        self.assertIn("Backend недоступен", self.source)
        self.assertIn("const canClearHistory = computed", self.source)
        self.assertIn("method: 'DELETE'", self.source)
        self.assertIn("Очистить историю ставок", self.source)

    def test_live_mode_requires_manual_confirmation(self):
        self.assertIn('@click="startMode(\'LIVE\')"', self.source)
        self.assertIn('@click="startMode(\'DEMO\')"', self.source)
        self.assertIn("READY_FOR_MANUAL_CONFIRMATION", self.source)
        self.assertIn("кнопку «Сделать ставку» нажимает пользователь", self.source)
        self.assertIn("const canClearDatabase = computed", self.source)
        self.assertIn("const canResetSequence = computed", self.source)
        self.assertIn(':disabled="!canResetSequence"', self.source)
        self.assertIn("/api/demo/database", self.source)
        self.assertIn("Очистить базу данных", self.source)
        self.assertIn("window.confirm", self.source)

    def test_match_filter_checkboxes_use_persisted_strategy_config(self):
        self.assertIn('v-model="strategyConfig.exclude_teams_enabled"', self.source)
        self.assertIn('v-model="strategyConfig.min_initial_odds_enabled"', self.source)
        self.assertIn("async function saveMatchFilters()", self.source)
        self.assertIn("api('/api/demo/strategy-config'", self.source)
        self.assertIn('@change="saveMatchFilters"', self.source)

    def test_strategy_picker_is_persisted_and_total_even_is_demo_only(self):
        self.assertIn("selectStrategyType('NEXT_GOAL')", self.source)
        self.assertIn("selectStrategyType('TOTAL_EVEN')", self.source)
        self.assertIn("strategyConfig.value.strategy_type", self.source)
        self.assertIn("Стратегия «Тотал чёт» пока доступна только в DEMO.", self.source)
        self.assertIn("item.strategy_name", self.source)

    def test_forks_tactic_links_to_separate_route(self):
        self.assertIn('to="/forks"', self.source)
        self.assertIn("Вилки", self.source)
        self.assertIn("Настольный теннис", self.source)
        self.assertIn("path: '/forks'", ROUTER.read_text(encoding="utf-8"))

    def test_forks_view_uses_read_only_scanner_api(self):
        source = FORKS_VIEW.read_text(encoding="utf-8")
        self.assertIn("/api/table-tennis/scan", source)
        self.assertIn("/api/table-tennis/start", source)
        self.assertIn("/api/table-tennis/stop", source)
        self.assertIn("Запустить DEMO вилки", source)
        self.assertIn("Остановить", source)
        self.assertIn("/api/table-tennis/leagues", source)
        self.assertIn("/api/table-tennis/matches", source)
        self.assertNotIn("stake", source.casefold())


if __name__ == "__main__":
    unittest.main()
