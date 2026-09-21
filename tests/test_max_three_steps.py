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
        self.assertIn("A", saved["blocked_match_ids"])
        events = [item["event"] for item in logs]
        self.assertIn("MAX_3_STEPS_LIMIT_REACHED", events)
        self.assertIn("MAX_3_STEPS_SWITCHING_MATCH", events)

        state_payload = state_update.await_args.kwargs
        self.assertEqual(state_payload["bet"]["step"], 4)
        self.assertEqual(state_payload["bet"]["amount"], 283)
        self.assertEqual(state_payload["bet"]["status"], "WAITING_NEXT_MATCH")


if __name__ == "__main__":
    unittest.main()
