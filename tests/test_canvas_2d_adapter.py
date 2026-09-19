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

    def test_vector_path_inside_button_marks_current_lock(self):
        snapshot = self.snapshot()
        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None
        vector_lock = {
            "seq": 20,
            "timestamp_ms": 1000,
            "canvas_id": 1,
            "generation": 0,
            "kind": "fill",
            "event_type": "path",
            "x": 575,
            "y": 99,
            "width": 13,
            "height": 19,
            "path_op_count": 7,
            "alpha": 1.0,
        }
        snapshot["paths"] = [vector_lock]
        snapshot["events"] = [vector_lock]
        snapshot["snapshot_at_ms"] = 1200

        lock_state = detect_lock_state(
            snapshot,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
        )

        self.assertEqual(lock_state["locked_sides"], (2,))
        self.assertEqual(lock_state["markers"]["2"]["reason"], "canvas-vector-icon")

    def test_transient_vector_lock_survives_unlock_redraw(self):
        snapshot = self.snapshot()
        snapshot["texts"][-1]["seq"] = 30
        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None
        before_odds = dict(snapshot["texts"][-1])
        before_odds["seq"] = 10
        before_odds["timestamp_ms"] = 900
        before_odds["event_type"] = "text"
        vector_lock = {
            "seq": 20,
            "timestamp_ms": 1000,
            "canvas_id": 1,
            "generation": 0,
            "kind": "stroke",
            "event_type": "path",
            "x": 575,
            "y": 99,
            "width": 13,
            "height": 19,
            "path_op_count": 6,
            "alpha": 1.0,
        }
        after_odds = dict(snapshot["texts"][-1])
        after_odds["seq"] = 30
        after_odds["timestamp_ms"] = 1100
        after_odds["event_type"] = "text"
        snapshot["paths"] = [vector_lock]
        snapshot["events"] = [before_odds, vector_lock, after_odds]
        snapshot["snapshot_at_ms"] = 1200

        lock_state = detect_lock_state(
            snapshot,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
        )

        self.assertEqual(lock_state["locked_sides"], ())
        self.assertEqual(lock_state["recent_locked_sides"], (2,))
        self.assertEqual(
            lock_state["recent_markers"]["2"]["reason"],
            "canvas-vector-icon",
        )

    def test_reads_internal_canvas_and_projects_to_visible_canvas(self):
        source = self.snapshot()
        source_layer = {
            "id": 1,
            "width": 1000,
            "height": 500,
            "class_name": "market-grid-canvas__offscreen-canvas",
            "texts": source["texts"],
            "rects": source["rects"],
            "images": [],
        }
        visible_layer = {
            "id": 2,
            "width": 500,
            "height": 250,
            "class_name": "market-grid-canvas__canvas",
            "texts": [],
            "rects": [],
            "images": [
                {
                    "seq": 20,
                    "source_canvas_id": 1,
                    "source_x": 0,
                    "source_y": 0,
                    "source_width": 1000,
                    "source_height": 500,
                    "x": 0,
                    "y": 0,
                    "width": 500,
                    "height": 250,
                }
            ],
        }
        snapshot = {
            "status": "READY",
            "selected_canvas_id": 2,
            "canvas": {
                "id": 2,
                "width": 500,
                "height": 250,
                "css_width": 500,
                "css_height": 250,
            },
            "texts": [],
            "rects": [],
            "images": visible_layer["images"],
            "canvases": [source_layer, visible_layer],
        }

        mapping = map_next_goal_snapshot(snapshot, 3)

        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(mapping.source_canvas_id, 1)
        self.assertEqual(mapping.team1_odds, 1.935)
        self.assertEqual(mapping.team2_odds, 2.04)
        self.assertEqual(
            mapping.team1_click_region,
            {"x": 105.0, "y": 44.0, "width": 65.0, "height": 22.0},
        )
        self.assertEqual(
            mapping.team2_click_region,
            {"x": 277.5, "y": 44.0, "width": 65.0, "height": 22.0},
        )


if __name__ == "__main__":
    unittest.main()
