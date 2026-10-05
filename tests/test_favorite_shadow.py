import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, Scorer, ScoreboardSnapshot
from backend.app.demo.strategy import StrategyConfig
from backend.app.favorite_shadow import (
    FAVORITE_SHADOW_MAX_STEPS,
    FavoriteShadowRuntime,
    select_favorite_with_lower_odds,
)


class FavoriteSelectionTests(unittest.TestCase):
    def test_selects_strict_lower_odds_as_favorite(self):
        selection = select_favorite_with_lower_odds(
            "Alpha",
            "Beta",
            NextGoalOdds(1.82, 2.08),
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.selected_team, "Alpha")
        self.assertEqual(selection.selected_side, Scorer.TEAM_1)
        self.assertEqual(selection.selected_odds, 1.82)

        selection = select_favorite_with_lower_odds(
            "Alpha",
            "Beta",
            NextGoalOdds(2.15, 1.77),
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.selected_team, "Beta")
        self.assertEqual(selection.selected_side, Scorer.TEAM_2)
        self.assertEqual(selection.selected_odds, 1.77)

    def test_equal_odds_have_no_favorite(self):
        self.assertIsNone(
            select_favorite_with_lower_odds(
                "Alpha",
                "Beta",
                NextGoalOdds(1.95, 1.95),
            )
        )

    def test_config_is_independent_and_disabled_by_default(self):
        disabled = StrategyConfig.from_payload({})
        enabled = StrategyConfig.from_payload({"favorite_shadow_enabled": True})
        self.assertFalse(disabled.favorite_shadow_enabled)
        self.assertTrue(enabled.favorite_shadow_enabled)
        self.assertTrue(enabled.to_dict()["favorite_shadow_enabled"])


class FavoriteShadowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_matches_can_be_observed_concurrently(self):
        runtime = FavoriteShadowRuntime()
        await runtime.load(enabled=True, mode="DEMO")

        await runtime.start_observation(
            match_id="A",
            match_name="Alpha — Beta",
            team1="Alpha",
            team2="Beta",
            favorite_team="Alpha",
            favorite_side="TEAM_1",
            initial_favorite_odds=1.80,
            outsider_team="Beta",
            initial_outsider_odds=2.10,
        )
        snapshot = await runtime.start_observation(
            match_id="B",
            match_name="Gamma — Delta",
            team1="Gamma",
            team2="Delta",
            favorite_team="Delta",
            favorite_side="TEAM_2",
            initial_favorite_odds=1.75,
            outsider_team="Gamma",
            initial_outsider_odds=2.15,
        )
        self.assertEqual(len(snapshot["active_observations"]), 2)

        await runtime.begin_step(
            match_id="A",
            step=1,
            score_before="0:0",
            favorite_odds=1.80,
            market_ready=True,
        )
        await runtime.record_goal(
            match_id="A",
            step=1,
            result="LOSE",
            score_before="0:0",
            score_after="0:1",
            scorer="Beta",
            favorite_odds=1.80,
            market_ready=True,
        )
        await runtime.begin_step(
            match_id="A",
            step=2,
            score_before="0:1",
            favorite_odds=None,
            market_ready=False,
        )
        snapshot = await runtime.record_goal(
            match_id="A",
            step=2,
            result="WIN",
            score_before="0:1",
            score_after="1:1",
            scorer="Alpha",
            favorite_odds=None,
            market_ready=False,
        )

        self.assertNotIn("A", snapshot["active_observations"])
        self.assertIn("B", snapshot["active_observations"])
        self.assertEqual(snapshot["observations"][-1]["winning_step"], 2)
        self.assertEqual(
            [step["result"] for step in snapshot["observations"][-1]["steps"]],
            ["LOSE", "WIN"],
        )
        self.assertEqual(snapshot["stats"]["wins_by_step"]["2"], 1)

    async def test_step_nine_without_favorite_goal_is_exhausted(self):
        runtime = FavoriteShadowRuntime()
        await runtime.load(enabled=True, mode="LIVE")
        await runtime.start_observation(
            match_id="A",
            match_name="Alpha — Beta",
            team1="Alpha",
            team2="Beta",
            favorite_team="Alpha",
            favorite_side="TEAM_1",
            initial_favorite_odds=1.75,
            outsider_team="Beta",
            initial_outsider_odds=2.20,
        )

        snapshot = None
        for step in range(1, FAVORITE_SHADOW_MAX_STEPS + 1):
            await runtime.begin_step(
                match_id="A",
                step=step,
                score_before=f"0:{step - 1}",
                favorite_odds=1.75,
                market_ready=True,
            )
            snapshot = await runtime.record_goal(
                match_id="A",
                step=step,
                result="LOSE",
                score_before=f"0:{step - 1}",
                score_after=f"0:{step}",
                scorer="Beta",
                favorite_odds=1.75,
                market_ready=True,
            )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["observations"][-1]["completion_status"], "EXHAUSTED")
        self.assertIsNone(snapshot["observations"][-1]["winning_step"])
        self.assertEqual(snapshot["stats"]["exhausted"], 1)

    async def test_restart_keeps_history_and_interrupts_active_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            runtime = FavoriteShadowRuntime(repository.save_favorite_shadow_runtime)
            await runtime.load(enabled=True, mode="DEMO")
            await runtime.start_observation(
                match_id="A",
                match_name="Alpha — Beta",
                team1="Alpha",
                team2="Beta",
                favorite_team="Alpha",
                favorite_side="TEAM_1",
                initial_favorite_odds=1.80,
                outsider_team="Beta",
                initial_outsider_odds=2.10,
            )

            restored = FavoriteShadowRuntime(repository.save_favorite_shadow_runtime)
            snapshot = await restored.load(
                enabled=True,
                mode="DEMO",
                persisted=await repository.get_favorite_shadow_runtime("DEMO"),
            )

        self.assertEqual(snapshot["active_observations"], {})
        self.assertEqual(snapshot["observations"][-1]["completion_status"], "INTERRUPTED")
        self.assertEqual(
            snapshot["observations"][-1]["interruption_reason"],
            "BACKEND_RESTART",
        )


class _FakePage:
    def __init__(self):
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))


class _FakeAuxManager:
    def __init__(self):
        self.page = _FakePage()
        self.closed = False

    def set_logger(self, _logger):
        return None

    async def create_aux_page(self):
        return self.page

    async def close_aux_page(self, page):
        self.closed = page is self.page


class _ReadyLeague:
    def __init__(self, _page, **_kwargs):
        pass

    async def wait_match_content_ready(self, **_kwargs):
        return {"ready": True}


class FavoriteShadowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_shadow_records_favorite_without_bet_history(self):
        zero = ScoreboardSnapshot("Alpha", "Beta", Score(0, 0), "00:10", "1-й тайм")
        outsider_goal = ScoreboardSnapshot(
            "Alpha", "Beta", Score(0, 1), "00:20", "1-й тайм"
        )
        favorite_goal = ScoreboardSnapshot(
            "Alpha", "Beta", Score(1, 1), "00:30", "1-й тайм"
        )

        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            manager = _FakeAuxManager()
            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.STATE.update", AsyncMock()),
                patch("backend.app.demo.engine.LeagueBrowser", _ReadyLeague),
            ):
                engine = DemoEngine(manager)
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {"favorite_shadow_enabled": True}
                )
                await engine._favorite_shadow.load(enabled=True, mode="DEMO")
                engine._favorite_shadow_initial_snapshot_and_odds = AsyncMock(
                    return_value=(zero, NextGoalOdds(1.80, 2.10))
                )
                engine._favorite_shadow_read_step_odds = AsyncMock(
                    return_value=(1.82, True)
                )
                engine._favorite_shadow_wait_for_goal = AsyncMock(
                    side_effect=[
                        (outsider_goal, Scorer.TEAM_2),
                        (favorite_goal, Scorer.TEAM_1),
                    ]
                )

                await engine._run_favorite_shadow_observer(
                    match_id="A",
                    match_name="Alpha — Beta",
                    match_url="https://example.test/match/A",
                    team1="Alpha",
                    team2="Beta",
                )

                history = await repository.history(mode="DEMO")
                runtime = await engine._favorite_shadow.snapshot()

        self.assertEqual(history, [])
        self.assertTrue(manager.closed)
        self.assertEqual(runtime["observations"][-1]["winning_step"], 2)
        self.assertEqual(
            [step["result"] for step in runtime["observations"][-1]["steps"]],
            ["LOSE", "WIN"],
        )


if __name__ == "__main__":
    unittest.main()
