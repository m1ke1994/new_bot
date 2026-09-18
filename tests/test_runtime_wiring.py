import unittest
from pathlib import Path


class RuntimeWiringTests(unittest.TestCase):
    def test_main_uses_canvas_2d_reader_for_next_goal(self):
        source = Path("backend/app/main.py").read_text(encoding="utf-8")
        self.assertIn("backend.app.browser.canvas_2d_adapter", source)
        self.assertIn(
            "demo_engine_module.read_next_goal_odds = canvas_2d_read_next_goal_odds",
            source,
        )
        self.assertNotIn("canvas_locking_adapter", source)

    def test_next_goal_wait_state_reports_canvas_2d(self):
        state_source = Path("backend/app/demo/state.py").read_text(encoding="utf-8")
        engine_source = Path("backend/app/demo/engine.py").read_text(encoding="utf-8")
        self.assertIn("CANVAS 2D / fillText + DOM fallback", state_source)
        self.assertIn("Canvas 2D / DOM", engine_source)
        self.assertIn("Canvas 2D / fillText", engine_source)

    def test_legacy_market_module_is_not_wired_as_runtime_reader(self):
        main_source = Path("backend/app/main.py").read_text(encoding="utf-8")
        self.assertNotIn("visual_lock_read_next_goal_odds", main_source)
        self.assertIn("canvas_2d_read_next_goal_odds", main_source)


if __name__ == "__main__":
    unittest.main()
