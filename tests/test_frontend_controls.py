import unittest
from pathlib import Path


APP_VUE = Path(__file__).parents[1] / "front" / "frontend" / "src" / "App.vue"


class FrontendControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_VUE.read_text(encoding="utf-8")

    def test_start_and_stop_are_driven_by_backend_state(self):
        self.assertIn(
            "const canStart = computed(() => backendAvailable.value && !initialLoading.value && !actionPending.value && !state.value.running",
            self.source,
        )
        self.assertIn(
            "const canStop = computed(() => backendAvailable.value && !actionPending.value && state.value.running)",
            self.source,
        )
        self.assertIn(':disabled="!canStart"', self.source)
        self.assertIn(':disabled="!canStop"', self.source)

    def test_backend_unavailable_and_clear_history_ux_exist(self):
        self.assertIn("Backend недоступен", self.source)
        self.assertIn("const canClearHistory = computed", self.source)
        self.assertIn("method: 'DELETE'", self.source)
        self.assertIn("Очистить историю ставок", self.source)

    def test_live_mode_requires_manual_confirmation(self):
        self.assertIn('<option value="LIVE">LIVE</option>', self.source)
        self.assertIn("READY_FOR_MANUAL_CONFIRMATION", self.source)
        self.assertIn("кнопку «Сделать ставку» нажимает пользователь", self.source)
        self.assertIn("const canClearDatabase = computed", self.source)
        self.assertIn("const canResetSequence = computed", self.source)
        self.assertIn(':disabled="!canResetSequence"', self.source)
        self.assertIn("/api/demo/database", self.source)
        self.assertIn("Очистить базу данных", self.source)
        self.assertIn("window.confirm", self.source)


if __name__ == "__main__":
    unittest.main()
