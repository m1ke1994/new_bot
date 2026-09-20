import unittest
from unittest.mock import patch

from backend.app.browser.canvas_2d_adapter import (
    _CachedMarket,
    _LAST_MARKETS,
    _detect_multilayer_lock_state,
    _LAST_REPORTED_LOCK_SEQ,
    _consume_recent_lock_sides,
    detect_lock_state,
    map_next_goal_snapshot,
    read_next_goal_lock_state,
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

    def test_transient_lock_is_seq_consumed_without_age_expiry(self):
        snapshot = self.snapshot()
        snapshot["generation"] = 7
        snapshot["texts"][-1]["seq"] = 30
        snapshot["texts"][-1]["generation"] = 7
        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None
        before_odds = dict(snapshot["texts"][-1])
        before_odds.update(
            seq=10,
            timestamp_ms=100,
            event_type="text",
            generation=7,
        )
        vector_lock = {
            "seq": 20,
            "timestamp_ms": 200,
            "canvas_id": 4,
            "generation": 7,
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
        after_odds.update(
            seq=30,
            timestamp_ms=300,
            event_type="text",
            generation=7,
        )
        snapshot["paths"] = []
        snapshot["events"] = [before_odds, vector_lock, after_odds]
        snapshot["snapshot_at_ms"] = 30_000

        lock_state = detect_lock_state(
            snapshot,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
        )
        page = type("Page", (), {"url": "https://example.test/match-1"})()
        _LAST_REPORTED_LOCK_SEQ.clear()

        first_sides, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )
        second_sides, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )

        self.assertEqual(lock_state["recent_locked_sides"], (2,))
        self.assertEqual(first_sides, (2,))
        self.assertEqual(second_sides, ())

    def test_lock_cursor_resets_for_generation_match_and_browser_context(self):
        page = type("Page", (), {"url": "https://example.test/match-1"})()
        marker = {
            "seq": 50,
            "canvas_id": 4,
            "generation": 1,
        }
        lock_state = {
            "locked_sides": (),
            "recent_locked_sides": (2,),
            "markers": {},
            "recent_markers": {"2": marker},
        }
        _LAST_REPORTED_LOCK_SEQ.clear()

        first, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )
        marker["generation"] = 2
        next_generation, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )
        page.url = "https://example.test/match-2"
        next_match, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )
        marker["hook_installed_at"] = 1000
        new_browser_context, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=3,
        )
        next_market, _ = _consume_recent_lock_sides(
            page,
            lock_state,
            market_context=4,
        )

        self.assertEqual(first, (2,))
        self.assertEqual(next_generation, (2,))
        self.assertEqual(next_match, (2,))
        self.assertEqual(new_browser_context, (2,))
        self.assertEqual(next_market, (2,))


    def test_lock_drawn_before_odds_in_same_render_burst_is_still_current(self):
        snapshot = self.snapshot()
        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None

        snapshot["texts"][-1]["seq"] = 21
        snapshot["texts"][-1]["timestamp_ms"] = 1050
        snapshot["texts"][-1]["generation"] = 0

        lock = text(
            "\ue91d",
            575,
            101,
            width=16,
            height=18,
            seq=20,
            font="16px icomoon",
        )
        lock["timestamp_ms"] = 1000
        lock["generation"] = 0
        lock["event_type"] = "text"

        snapshot["texts"].append(lock)
        snapshot["snapshot_at_ms"] = 1100

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

    def test_multilayer_lock_left_of_label_matches_real_market_layout(self):
        source = self.snapshot()

        source_layer = {
            "id": 1,
            "width": 1000,
            "height": 500,
            "class_name": "odds-layer",
            "texts": source["texts"],
            "rects": source["rects"],
            "images": [],
            "paths": [],
            "events": [],
            "snapshot_at_ms": 1200,
        }

        # Real bookmaker layout: lock is rendered to the LEFT of
        # "Команда 2 - N-й гол", far away from the odds text on the right.
        overlay_lock = {
            "seq": 40,
            "timestamp_ms": 1100,
            "generation": 0,
            "kind": "drawImage",
            "event_type": "image",
            "x": 318,
            "y": 96,
            "width": 12,
            "height": 14,
            "source": {"src": "sprite.png"},
            "alpha": 1.0,
        }

        overlay_layer = {
            "id": 3,
            "width": 1000,
            "height": 500,
            "class_name": "overlay-layer",
            "texts": [],
            "rects": [],
            "images": [overlay_lock],
            "paths": [],
            "events": [overlay_lock],
            "snapshot_at_ms": 1200,
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
                    "seq": 50,
                    "source_canvas_id": 1,
                    "source_x": 0,
                    "source_y": 0,
                    "source_width": 1000,
                    "source_height": 500,
                    "x": 0,
                    "y": 0,
                    "width": 500,
                    "height": 250,
                },
                {
                    "seq": 51,
                    "source_canvas_id": 3,
                    "source_x": 0,
                    "source_y": 0,
                    "source_width": 1000,
                    "source_height": 500,
                    "x": 0,
                    "y": 0,
                    "width": 500,
                    "height": 250,
                },
            ],
            "paths": [],
            "events": [],
            "snapshot_at_ms": 1200,
        }

        snapshot = {
            "status": "READY",
            "selected_canvas_id": 2,
            "snapshot_at_ms": 1200,
            "canvas": {"id": 2, "width": 500, "height": 250},
            "texts": [],
            "rects": [],
            "images": visible_layer["images"],
            "paths": [],
            "events": [],
            "canvases": [source_layer, overlay_layer, visible_layer],
        }

        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None

        lock_state = _detect_multilayer_lock_state(
            snapshot,
            source_canvas_id=mapping.source_canvas_id,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
            team1_click_region=mapping.team1_click_region,
            team2_click_region=mapping.team2_click_region,
        )

        self.assertEqual(lock_state["locked_sides"], (2,))
        self.assertEqual(
            lock_state["markers"]["2"]["reason"],
            "canvas-image-icon",
        )
        self.assertEqual(
            lock_state["markers"]["2"]["detected_canvas_id"],
            3,
        )

    def test_multilayer_overlay_lock_is_detected_outside_odds_canvas(self):
        source = self.snapshot()

        source_layer = {
            "id": 1,
            "width": 1000,
            "height": 500,
            "class_name": "odds-layer",
            "texts": source["texts"],
            "rects": source["rects"],
            "images": [],
            "paths": [],
            "events": [],
            "snapshot_at_ms": 1200,
        }

        overlay_lock = {
            "seq": 40,
            "timestamp_ms": 1100,
            "generation": 0,
            "kind": "drawImage",
            "event_type": "image",
            "x": 650,
            "y": 91,
            "width": 12,
            "height": 14,
            "source": {"src": "sprite.png"},
            "alpha": 1.0,
        }

        overlay_layer = {
            "id": 3,
            "width": 1000,
            "height": 500,
            "class_name": "overlay-layer",
            "texts": [],
            "rects": [],
            "images": [overlay_lock],
            "paths": [],
            "events": [overlay_lock],
            "snapshot_at_ms": 1200,
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
                    "seq": 50,
                    "source_canvas_id": 1,
                    "source_x": 0,
                    "source_y": 0,
                    "source_width": 1000,
                    "source_height": 500,
                    "x": 0,
                    "y": 0,
                    "width": 500,
                    "height": 250,
                },
                {
                    "seq": 51,
                    "source_canvas_id": 3,
                    "source_x": 0,
                    "source_y": 0,
                    "source_width": 1000,
                    "source_height": 500,
                    "x": 0,
                    "y": 0,
                    "width": 500,
                    "height": 250,
                },
            ],
            "paths": [],
            "events": [],
            "snapshot_at_ms": 1200,
        }

        snapshot = {
            "status": "READY",
            "selected_canvas_id": 2,
            "snapshot_at_ms": 1200,
            "canvas": {
                "id": 2,
                "width": 500,
                "height": 250,
            },
            "texts": [],
            "rects": [],
            "images": visible_layer["images"],
            "paths": [],
            "events": [],
            "canvases": [
                source_layer,
                overlay_layer,
                visible_layer,
            ],
        }

        mapping = map_next_goal_snapshot(snapshot, 3)
        assert mapping is not None

        lock_state = _detect_multilayer_lock_state(
            snapshot,
            source_canvas_id=mapping.source_canvas_id,
            team1_region=mapping.team1_region,
            team2_region=mapping.team2_region,
            team1_click_region=mapping.team1_click_region,
            team2_click_region=mapping.team2_click_region,
        )

        self.assertEqual(lock_state["locked_sides"], (2,))
        self.assertEqual(
            lock_state["markers"]["2"]["reason"],
            "canvas-image-icon",
        )
        self.assertEqual(
            lock_state["markers"]["2"]["detected_canvas_id"],
            3,
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


class Canvas2DAdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_lock_state_passes_goal_as_market_context(self):
        page = type("Page", (), {"url": "https://example.test/match-1"})()
        region = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}
        _LAST_MARKETS[id(page)] = _CachedMarket(
            url=page.url,
            next_goal_number=2,
            team1_odds=1.90,
            team2_odds=1.95,
            team1_region=region,
            team2_region=region,
            team1_click_region=region,
            team2_click_region=region,
            source_canvas_id=1,
            canvas={"width": 1000, "height": 500},
        )
        seen = {}
        lock_state = {
            "locked_sides": (),
            "recent_locked_sides": (),
            "markers": {},
            "recent_markers": {},
            "checked_canvas_ids": (1, 2, 3),
        }

        async def fake_snapshot(_page):
            return {"status": "READY"}

        def fake_detect(*_args, **_kwargs):
            return lock_state

        def fake_consume(_page, _state, *, market_context):
            seen["market_context"] = market_context
            return (), {}

        try:
            with (
                patch(
                    "backend.app.browser.canvas_2d_adapter.capture_canvas_2d_snapshot",
                    side_effect=fake_snapshot,
                ),
                patch(
                    "backend.app.browser.canvas_2d_adapter._detect_multilayer_lock_state",
                    side_effect=fake_detect,
                ),
                patch(
                    "backend.app.browser.canvas_2d_adapter._consume_recent_lock_sides",
                    side_effect=fake_consume,
                ),
            ):
                state = await read_next_goal_lock_state(page, 2)
        finally:
            _LAST_MARKETS.pop(id(page), None)

        self.assertEqual(seen["market_context"], 2)
        self.assertTrue(state["available"])
        self.assertEqual(state["checked_canvas_ids"], (1, 2, 3))


if __name__ == "__main__":
    unittest.main()
