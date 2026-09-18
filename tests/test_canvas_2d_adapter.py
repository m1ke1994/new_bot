import unittest

from backend.app.browser.canvas_2d_adapter import (
    detect_lock_state,
    map_next_goal_snapshot,
)


def text(text_value, x, y, width=90, height=18, seq=1, font="14px Arial"):
    return {
        "text": text_value,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "anchor_x": x,
        "anchor_y": y + height,
        "seq": seq,
        "kind": "fillText",
        "font": font,
        "alpha": 1.0,
    }


def rect(x, y, width, height, seq=1):
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "seq": seq,
        "kind": "roundRect",
        "alpha": 1.0,
    }


class Canvas2DAdapterTests(unittest.TestCase):
    def snapshot(self, *, lock_team2=False):
        texts = [
            text("Команда 1 - 3-й гол", 30, 100, width=150, seq=1),
            text("1.935", 260, 100, width=55, seq=2),
            text("Команда 2 - 3-й гол", 340, 100, width=160, seq=3),
            text("2.04", 610, 100, width=45, seq=4),
        ]
        if lock_team2:
            texts.append(
                text("\ue91d", 575, 101, width=16, height=18, seq=5, font="16px icomoon")
            )
        return {
            "status": "READY",
            "canvas": {
                "width": 1000,
                "height": 500,
                "css_width": 1000,
                "css_height": 500,
            },
            "texts": texts,
            "rects": [
                rect(210, 88, 130, 44, seq=10),
                rect(555, 88, 130, 44, seq=11),
            ],
            "images": [],
        }

    def test_maps_direct_fill_text_to_odds_and_button_rects(self):
        mapping = map_next_goal_snapshot(self.snapshot(), 3)

        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(mapping.team1_odds, 1.935)
        self.assertEqual(mapping.team2_odds, 2.04)
        self.assertEqual(
            mapping.team1_region,
            {"x": 210.0, "y": 88.0, "width": 130.0, "height": 44.0},
        )
        self.assertEqual(
            mapping.team2_region,
            {"x": 555.0, "y": 88.0, "width": 130.0, "height": 44.0},
        )

    def test_rejects_row_for_different_goal_number(self):
        self.assertIsNone(map_next_goal_snapshot(self.snapshot(), 4))

    def test_private_icon_glyph_inside_button_marks_locked_side(self):
        snapshot = self.snapshot(lock_team2=True)
        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None

        lock_state = detect_lock_state(
            snapshot,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
        )

        self.assertEqual(lock_state["locked_sides"], (2,))
        self.assertEqual(
            lock_state["markers"]["2"]["reason"],
            "private-icon-glyph",
        )


if __name__ == "__main__":
    unittest.main()
