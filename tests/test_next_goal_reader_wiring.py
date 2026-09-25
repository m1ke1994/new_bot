import unittest

from backend.app.demo import engine as engine_module


class NextGoalReaderWiringTests(unittest.TestCase):
    def test_runtime_reader_is_canvas_2d_adapter(self):
        self.assertEqual(
            engine_module.read_next_goal_odds.__module__,
            "backend.app.browser.canvas_2d_adapter",
        )

    def test_runtime_reader_is_not_legacy_market_reader(self):
        self.assertNotEqual(
            engine_module.read_next_goal_odds.__module__,
            "backend.app.browser.market",
        )


if __name__ == "__main__":
    unittest.main()
