import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.models import NextGoalOdds, Score, Scorer, ScoreboardSnapshot, TeamSelection
from backend.app.demo.strategy import StrategyConfig
from backend.app.team1_profile import (
    Team1ProfileRuntime,
    is_team1_profile_allowed,
    rejection_reason,
)


class Team1ProfileRuleTests(unittest.TestCase):
    def test_allows_only_team1_below_exclusive_threshold(self):
        self.assertTrue(
            is_team1_profile_allowed("TEAM_1", 1.99, enabled=True, max_odds=2.00)
        )
        self.assertFalse(
            is_team1_profile_allowed("TEAM_1", 2.00, enabled=True, max_odds=2.00)
        )
        self.assertFalse(
            is_team1_profile_allowed("TEAM_2", 1.80, enabled=True, max_odds=2.00)
        )
        self.assertTrue(
            is_team1_profile_allowed("TEAM_2", 2.50, enabled=False, max_odds=2.00)
        )

    def test_rejection_reason_distinguishes_side_and_odds(self):
        self.assertEqual(
            rejection_reason("TEAM_2", 1.95, max_odds=2.00),
            "SELECTED_TEAM_IS_TEAM_2",
        )
        self.assertEqual(
            rejection_reason("TEAM_1", 2.00, max_odds=2.00),
            "ODDS_NOT_BELOW_MAX",
        )
        self.assertEqual(
            rejection_reason("TEAM_2", 2.10, max_odds=2.00),
            "NOT_TEAM_1_AND_ODDS_NOT_BELOW_MAX",
        )

    def test_strategy_config_has_independent_profile_settings(self):
        disabled = StrategyConfig.from_payload({})
        enabled = StrategyConfig.from_payload(
            {"team1_profile_enabled": True, "team1_profile_max_odds": 1.985}
        )
        self.assertFalse(disabled.team1_profile_enabled)
        self.assertEqual(disabled.to_dict()["team1_profile_max_odds"], 2.0)
        self.assertTrue(enabled.team1_profile_enabled)
        self.assertEqual(enabled.to_dict()["team1_profile_max_odds"], 1.99)


class Team1ProfileRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_observation_is_persisted_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            runtime = Team1ProfileRuntime(repository.save_team1_profile_runtime)
            await runtime.load(
                enabled=True,
                max_odds=2.00,
                mode="DEMO",
                persisted=await repository.get_team1_profile_runtime("DEMO"),
            )
            await runtime.start_observation(
                match_id="A",
                match_name="Alpha — Beta",
                selected_team="Beta",
                selected_side="TEAM_2",
                initial_odds=1.95,
                reason="SELECTED_TEAM_IS_TEAM_2",
            )
            await runtime.begin_step(
                match_id="A", step=1, current_odds=1.95, score_before="0:0"
            )
            await runtime.record_step(
                match_id="A",
                step=1,
                result="LOSE",
                current_odds=1.95,
                score_before="0:0",
                score_after="1:0",
                scorer="Alpha",
            )
            await runtime.begin_step(
                match_id="A", step=2, current_odds=1.92, score_before="1:0"
            )
            completed = await runtime.record_step(
                match_id="A",
                step=2,
                result="WIN",
                current_odds=1.92,
                score_before="1:0",
                score_after="1:1",
                scorer="Beta",
            )

            self.assertEqual(completed["state"], "READY")
            self.assertIsNone(completed["active_observation"])
            self.assertEqual(completed["observations"][-1]["series_length"], 2)
            self.assertEqual(
                completed["observations"][-1]["filter_reason"],
                "SELECTED_TEAM_IS_TEAM_2",
            )

            restored = Team1ProfileRuntime(repository.save_team1_profile_runtime)
            snapshot = await restored.load(
                enabled=True,
                max_odds=2.00,
                mode="DEMO",
                persisted=await repository.get_team1_profile_runtime("DEMO"),
            )

        self.assertEqual(len(snapshot["observations"]), 1)
        self.assertEqual(snapshot["observations"][0]["match_id"], "A")
        self.assertEqual(snapshot["observations"][0]["completion_status"], "COMPLETED")


class Team1ProfileEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_observation_never_creates_bet_history(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            manager = AsyncMock()
            manager.ensure_page.return_value = object()
            manager.set_logger = lambda _logger: None

            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.STATE.update", AsyncMock()),
            ):
                engine = DemoEngine(manager)
                engine._mode = "DEMO"
                engine._config = StrategyConfig.from_payload(
                    {
                        "team1_profile_enabled": True,
                        "team1_profile_max_odds": 2.00,
                        "max_steps": 4,
                    }
                )
                await engine._team1_profile.load(
                    enabled=True,
                    max_odds=2.00,
                    mode="DEMO",
                )
                await engine._team1_profile.start_observation(
                    match_id="A",
                    match_name="Alpha — Beta",
                    selected_team="Alpha",
                    selected_side="TEAM_1",
                    initial_odds=2.05,
                    reason="ODDS_NOT_BELOW_MAX",
                )

                zero = ScoreboardSnapshot(
                    "Alpha", "Beta", Score(0, 0), "00:10", "1-й тайм"
                )
                loss = ScoreboardSnapshot(
                    "Alpha", "Beta", Score(0, 1), "01:00", "1-й тайм"
                )
                win = ScoreboardSnapshot(
                    "Alpha", "Beta", Score(1, 1), "02:00", "1-й тайм"
                )
                engine._wait_for_goal = AsyncMock(
                    side_effect=[
                        (loss, Scorer.TEAM_2),
                        (win, Scorer.TEAM_1),
                    ]
                )
                engine._wait_for_odds = AsyncMock(
                    return_value=(
                        loss,
                        NextGoalOdds(2.02, 1.84, next_goal_number=2),
                    )
                )

                await engine._process_team1_profile_observation(
                    selected_match={
                        "match_id": "A",
                        "team1": "Alpha",
                        "team2": "Beta",
                    },
                    match_name="Alpha — Beta",
                    snapshot=zero,
                    initial_odds=NextGoalOdds(2.05, 1.80, next_goal_number=1),
                    selection=TeamSelection(
                        "Alpha", Scorer.TEAM_1, 2.05, "Beta", 1.80
                    ),
                )

                history = await repository.history(mode="DEMO")
                runtime = await engine._team1_profile.snapshot()

        self.assertEqual(history, [])
        self.assertEqual(runtime["state"], "READY")
        self.assertEqual(runtime["observations"][-1]["series_length"], 2)
        self.assertEqual(
            [item["result"] for item in runtime["observations"][-1]["steps"]],
            ["LOSE", "WIN"],
        )


if __name__ == "__main__":
    unittest.main()
