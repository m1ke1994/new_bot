import unittest

from backend.app.browser.canvas_vision import _map_next_goal_market


def label(text, x, y, *, confidence=0.98):
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": 82,
        "height": 10,
        "confidence": confidence,
        "engine": "RapidOCR",
        "variant": "test",
    }


def odd(value, x, y, *, confidence=0.99):
    return {
        "text": str(value),
        "value": float(value),
        "x": x,
        "y": y,
        "width": 42,
        "height": 20,
        "confidence": confidence,
        "engine": "RapidOCR",
        "variant": "test",
        "usable": True,
    }


class CanvasGoalRowMappingTests(unittest.TestCase):
    def test_adjacent_goal_labels_do_not_steal_current_odds_row(self):
        # Odds are vertically centred at y=110. The previous implementation used
        # their top y=100 as the row centre and could pick the preceding goal-3
        # labels (centre y=92) instead of the real goal-4 labels (centre y=110).
        team1_odd = odd(2.14, 220, 100)
        team2_odd = odd(1.71, 520, 100)
        regions = [
            label("Команда 1 - 3-й гол", 80, 87),
            label("Команда 2 - 3-й гол", 380, 87),
            label("Команда 1 - 4-й гол", 80, 105),
            label("Команда 2 - 4-й гол", 380, 105),
            team1_odd,
            team2_odd,
        ]
        headers = [{"x": 0, "y": 60, "width": 800, "height": 20}]

        mapping = _map_next_goal_market(
            regions,
            [team1_odd, team2_odd],
            headers,
            800,
            300,
            expected_goal_number=4,
        )

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["next_goal_number"], 4)
        self.assertEqual(mapping["team1"]["value"], 2.14)
        self.assertEqual(mapping["team2"]["value"], 1.71)

    def test_wrong_visible_goal_is_not_relabelled_as_expected_goal(self):
        team1_odd = odd(2.05, 220, 100)
        team2_odd = odd(1.78, 520, 100)
        regions = [
            label("Команда 1 - 3-й гол", 80, 105),
            label("Команда 2 - 3-й гол", 380, 105),
            team1_odd,
            team2_odd,
        ]
        headers = [{"x": 0, "y": 60, "width": 800, "height": 20}]

        mapping = _map_next_goal_market(
            regions,
            [team1_odd, team2_odd],
            headers,
            800,
            300,
            expected_goal_number=4,
        )

        self.assertIsNone(mapping)


if __name__ == "__main__":
    unittest.main()
