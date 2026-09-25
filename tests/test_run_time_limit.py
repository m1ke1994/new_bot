import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.strategy import StrategyConfig


class FakeManager:
    def set_logger(self, _logger):
        pass


class RunTimeLimitTests(unittest.IsolatedAsyncioTestCase):
    def _expired_engine(self) -> DemoEngine:
        engine = DemoEngine(FakeManager())
        engine._mode = "LIVE"
        engine._config = StrategyConfig.from_payload(
            {
                "run_time_limit_enabled": True,
                "run_duration_hours": 3,
            }
        )
        engine._run_started_monotonic = time.monotonic() - 20
        engine._run_deadline_monotonic = time.monotonic() - 1
        engine._run_started_at_iso = "2026-09-25T10:00:00+03:00"
        engine._run_deadline_at_iso = "2026-09-25T13:00:00+03:00"
        return engine

    async def test_expired_idle_session_stops_without_new_bet(self):
        engine = self._expired_engine()
        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()) as update,
        ):
            stopped = await engine._stop_for_run_time_limit_if_idle("TEST_IDLE")

        self.assertTrue(stopped)
        self.assertTrue(engine._stop_event.is_set())
        self.assertTrue(engine._run_limit_stopped)
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("RUN_TIME_LIMIT_REACHED", events)
        self.assertIn("RUN_TIME_LIMIT_STOPPED", events)
        self.assertTrue(
            any(
                call.kwargs.get("event") == "RUN_TIME_LIMIT_STOPPED"
                for call in update.await_args_list
            )
        )

    async def test_expired_session_waits_for_accepted_bet_then_stops(self):
        engine = self._expired_engine()
        engine._accepted_bet_in_progress = True
        with (
            patch("backend.app.demo.engine.REPOSITORY.log", AsyncMock()) as log,
            patch("backend.app.demo.engine.STATE.update", AsyncMock()),
        ):
            stopped_while_active = await engine._stop_for_run_time_limit_if_idle(
                "ACTIVE_BET"
            )
            self.assertFalse(stopped_while_active)
            self.assertFalse(engine._stop_event.is_set())

            engine._accepted_bet_in_progress = False
            stopped_after_settlement = await engine._stop_for_run_time_limit_if_idle(
                "AFTER_SETTLEMENT"
            )

        self.assertTrue(stopped_after_settlement)
        self.assertTrue(engine._stop_event.is_set())
        events = [call.args[0] for call in log.await_args_list]
        self.assertIn("RUN_TIME_LIMIT_WAITING_ACTIVE_BET", events)
        self.assertIn("RUN_TIME_LIMIT_STOPPED", events)

    def test_disabled_limit_never_expires(self):
        engine = DemoEngine(FakeManager())
        engine._config = StrategyConfig.from_payload(
            {
                "run_time_limit_enabled": False,
                "run_duration_hours": 24,
            }
        )
        engine._configure_run_time_limit()
        self.assertIsNone(engine._run_deadline_monotonic)
        self.assertFalse(engine._run_time_limit_reached())


if __name__ == "__main__":
    unittest.main()
