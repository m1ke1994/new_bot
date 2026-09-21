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

    def test_canvas_hook_is_deferred_until_market_reading(self):
        manager_source = Path("backend/app/browser/manager.py").read_text(
            encoding="utf-8"
        )
        adapter_source = Path(
            "backend/app/browser/canvas_2d_adapter.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn(
            "await context.add_init_script(CANVAS_2D_HOOK_SCRIPT)",
            manager_source,
        )
        self.assertIn("CANVAS_2D_HOOK_DEFERRED", manager_source)
        self.assertIn(
            "hook_installed_now = await ensure_canvas_2d_hook(page)",
            adapter_source,
        )
        self.assertIn("CANVAS_2D_HOOK_INSTALLED_LATE", adapter_source)
        self.assertNotIn(
            "await page.add_init_script(CANVAS_2D_HOOK_SCRIPT)",
            adapter_source,
        )

    def test_canvas_hook_tracks_renderer_diagnostics(self):
        source = Path("backend/app/browser/canvas_2d_adapter.py").read_text(
            encoding="utf-8"
        )
        for marker in (
            "transferControlToOffscreen",
            "putImageData",
            "createImageBitmap",
            "OffscreenCanvasRenderingContext2D",
            "CANVAS_2D_DIAGNOSTICS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
