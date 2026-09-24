import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.history import DemoRepository
from backend.app.demo.strategy import StrategyConfig


class FakeManager:
    def set_logger(self, logger):
        self.logger = logger


class MaxThreeStepsTests(unittest.IsolatedAsyncioTestCase):
    def test_config_defaults_off_and_roundtrips(self):
        disabled = StrategyConfig.from_payload({})
        enabled = StrategyConfig.from_payload({"max_three_steps_enabled": True})

        self.assertFalse(disabled.max_three_steps_enabled)
        self.assertTrue(enabled.max_three_steps_enabled)
        self.assertTrue(enabled.to_dict()["max_three_steps_enabled"])

    def test_live_max_three_counts_only_accepted_losses(self):
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {
                "max_steps": 7,
                "stakes": [10, 22, 48, 105, 231, 508, 1117],
                "max_three_steps_enabled": True,
            }
        )

        self.assertFalse(
            engine._max_three_switch_ready(
                result="NOT_PLACED",
                switch_already_used=False,
                accepted_losses_in_current_match=3,
                step=3,
            )
        )
        self.assertFalse(
            engine._max_three_switch_ready(
                result="LOSE",
                switch_already_used=False,
                accepted_losses_in_current_match=2,
                step=2,
            )
        )
        self.assertTrue(
            engine._max_three_switch_ready(
                result="LOSE",
                switch_already_used=False,
                accepted_losses_in_current_match=3,
                step=3,
            )
        )
        self.assertFalse(
            engine._max_three_switch_ready(
                result="LOSE",
                switch_already_used=True,
                accepted_losses_in_current_match=6,
                step=6,
            )
        )

    async def test_live_three_losses_moves_step_four_to_next_match(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            sequence = await repository.get_sequence()
            await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                current_match_id="LIVE-A",
                selected_team="Team A",
            )

            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch("backend.app.demo.engine.STATE.update", AsyncMock()),
            ):
                engine = DemoEngine(FakeManager())
                engine._mode = "LIVE"
                engine._config = StrategyConfig.from_payload(
                    {
                        "max_steps": 7,
                        "stakes": [10, 22, 48, 105, 231, 508, 1117],
                        "max_three_steps_enabled": True,
                    }
                )

                next_step = await engine._switch_match_after_three_losses(
                    selected_match={
                        "match_id": "LIVE-A",
                        "team1": "Team A",
                        "team2": "Team B",
                    },
                    match_name="Team A — Team B",
                    cycle_id=sequence["sequence_id"],
                    step=3,
                    losses_in_current_match=3,
                )

                saved = await repository.get_sequence()
                logs = await repository.logs()

        self.assertEqual(next_step, 4)
        self.assertEqual(saved["current_step"], 4)
        self.assertEqual(saved["status"], "WAITING_FOR_MATCH")
        self.assertEqual(saved["blocked_match_ids"], ["LIVE-A"])
        max_three_log = next(
            item for item in logs if item["event"] == "MAX_3_STEPS_LIMIT_REACHED"
        )
        self.assertIn("mode=LIVE", max_three_log["message"])
        self.assertIn("accepted_losses_in_match=3", max_three_log["message"])

    async def test_three_losses_switch_preserves_next_step_on_next_match(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            sequence = await repository.get_sequence()
            cycle_id = sequence["sequence_id"]
            await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                current_match_id="A",
                selected_team="Team A",
                cumulative_losses="188.00",
            )

            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch(
                    "backend.app.demo.engine.STATE.update",
                    AsyncMock(),
                ) as state_update,
            ):
                engine = DemoEngine(FakeManager())
                engine._config = StrategyConfig.from_payload(
                    {
                        "initial_stake": 27,
                        "progression_multiplier": 2,
                        "max_steps": 6,
                        "stakes": [27, 59, 129, 283, 622, 1368],
                        "max_three_steps_enabled": True,
                    }
                )

                next_step = await engine._switch_match_after_three_losses(
                    selected_match={
                        "match_id": "A",
                        "team1": "Team A",
                        "team2": "Team B",
                    },
                    match_name="Team A — Team B",
                    cycle_id=cycle_id,
                    step=3,
                    losses_in_current_match=3,
                )

                saved = await repository.get_sequence()
                logs = await repository.logs()

        self.assertEqual(next_step, 4)
        self.assertEqual(saved["current_step"], 4)
        self.assertEqual(saved["status"], "WAITING_FOR_MATCH")
        self.assertIsNone(saved["current_match_id"])
        self.assertIsNone(saved["selected_team"])
        self.assertEqual(saved["max_three_switched"], 1)
        self.assertIn("A", saved["blocked_match_ids"])
        events = [item["event"] for item in logs]
        self.assertIn("MAX_3_STEPS_LIMIT_REACHED", events)
        self.assertIn("MAX_3_STEPS_SWITCHING_MATCH", events)

        state_payload = state_update.await_args.kwargs
        self.assertEqual(state_payload["bet"]["step"], 4)
        self.assertEqual(state_payload["bet"]["amount"], 283)
        self.assertEqual(state_payload["bet"]["status"], "WAITING_NEXT_MATCH")

    async def test_second_max_three_switch_is_disabled_until_sequence_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            cycle_id = (await repository.get_sequence())["sequence_id"]
            await repository.save_sequence(
                current_step=3,
                status="ACTIVE",
                current_match_id="A",
                selected_team="Team A",
            )

            with (
                patch("backend.app.demo.engine.REPOSITORY", repository),
                patch(
                    "backend.app.demo.engine.STATE.update",
                    AsyncMock(),
                ),
            ):
                engine = DemoEngine(FakeManager())
                engine._config = StrategyConfig.from_payload(
                    {
                        "initial_stake": 27,
                        "progression_multiplier": 2,
                        "max_steps": 8,
                        "stakes": [27, 59, 129, 283, 622, 1368, 3000, 6600],
                        "max_three_steps_enabled": True,
                    }
                )

                await engine._switch_match_after_three_losses(
                    selected_match={
                        "match_id": "A",
                        "team1": "Team A",
                        "team2": "Team B",
                    },
                    match_name="Team A — Team B",
                    cycle_id=cycle_id,
                    step=3,
                    losses_in_current_match=3,
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "MAX_3_STEPS_SWITCH_ALREADY_USED",
                ):
                    await engine._switch_match_after_three_losses(
                        selected_match={
                            "match_id": "B",
                            "team1": "Team C",
                            "team2": "Team D",
                        },
                        match_name="Team C — Team D",
                        cycle_id=cycle_id,
                        step=6,
                        losses_in_current_match=3,
                    )

                after_first_switch = await repository.get_sequence()
                self.assertEqual(after_first_switch["current_step"], 4)
                self.assertEqual(after_first_switch["max_three_switched"], 1)
                self.assertNotIn("B", after_first_switch["blocked_match_ids"])

                reset = await repository.reset_sequence()

        self.assertEqual(reset["current_step"], 1)
        self.assertEqual(reset["max_three_switched"], 0)
        self.assertEqual(reset["blocked_match_ids"], [])


if __name__ == "__main__":
    unittest.main()
