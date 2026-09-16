import unittest
from pathlib import Path


class RuntimeWiringTests(unittest.TestCase):
    def test_main_keeps_engine_market_reader_intact(self):
        source = Path("backend/app/main.py").read_text(encoding="utf-8")
        self.assertNotIn("demo_engine_module.read_next_goal_odds", source)
        self.assertNotIn("canvas_aware_read_next_goal_odds", source)
        self.assertIn("from backend.app.demo.engine import ENGINE", source)

    def test_engine_uses_hybrid_market_reader(self):
        source = Path("backend/app/demo/engine.py").read_text(encoding="utf-8")
        self.assertIn("from backend.app.browser.market import (", source)
        self.assertIn("read_next_goal_odds,", source)

        market_source = Path("backend/app/browser/market.py").read_text(encoding="utf-8")
        self.assertIn("DOM-first and Canvas-Vision fallback", market_source)
        self.assertIn("_read_next_goal_odds_canvas", market_source)
        self.assertIn("_prepare_market_search", market_source)


if __name__ == "__main__":
    unittest.main()
