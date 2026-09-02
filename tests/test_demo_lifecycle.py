import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.app.demo.engine import DemoEngine
from backend.app.demo.state import STATE


class FakeBrowserManager:
    def __init__(self):
        self.stop_calls = 0
        self.generation = 1
        self.logger = None

    def set_logger(self, logger):
        self.logger = logger

    async def snapshot(self):
        return {"status": "OPEN", "context": "OPEN", "page": "OPEN"}

    async def stop(self):
        self.stop_calls += 1


class FakePage:
    pass


class DemoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_stop_does_not_stop_browser(self):
        manager = FakeBrowserManager()
        engine = DemoEngine(manager)

        async def worker():
            await engine._stop_event.wait()

        engine._stop_event.clear()
        engine._task = asyncio.create_task(worker())
        state = await engine.stop()

        self.assertEqual(manager.stop_calls, 0)
        self.assertEqual(state["browser"]["status"], "OPEN")
        self.assertFalse(state["running"])

    async def test_second_start_reuses_running_task(self):
        manager = FakeBrowserManager()
        engine = DemoEngine(manager)

        async def worker():
            await engine._stop_event.wait()

        engine._run_guarded = worker
        await engine.start()
        first_task = engine.task
        second_state = await engine.start()
        self.assertIs(engine.task, first_task)
        self.assertEqual(second_state["event"], "DEMO_ALREADY_RUNNING")
        await engine.stop()

    async def test_manual_login_status_transitions_to_authorized(self):
        manager = FakeBrowserManager()
        engine = DemoEngine(manager)
        observed_statuses = []

        async def manual_authorize(
            _page,
            _stop_event,
            on_waiting,
            on_authorized,
        ):
            await on_waiting()
            waiting_state = await STATE.snapshot()
            observed_statuses.append(waiting_state["status"])
            observed_statuses.append(waiting_state["auth"]["status"])
            await on_authorized()
            return {"ok": True, "status": "AUTHORIZED"}

        with patch("backend.app.demo.engine.authorize", new=manual_authorize):
            authorized = await engine._ensure_authorized(FakePage())

        state = await STATE.snapshot()
        self.assertTrue(authorized)
        self.assertEqual(
            observed_statuses,
            ["WAITING_MANUAL_LOGIN", "WAITING_MANUAL_LOGIN"],
        )
        self.assertEqual(state["status"], "AUTHORIZED")
        self.assertEqual(state["auth"]["status"], "AUTHORIZED")

    async def test_auth_timeout_still_allows_the_worker_to_continue(self):
        manager = FakeBrowserManager()
        engine = DemoEngine(manager)

        async def timeout_authorize(
            _page,
            _stop_event,
            on_waiting,
            _on_authorized,
        ):
            await on_waiting()
            return {"ok": True, "status": "AUTH_TIMEOUT"}

        with patch("backend.app.demo.engine.authorize", new=timeout_authorize):
            should_continue = await engine._ensure_authorized(FakePage())

        state = await STATE.snapshot()
        self.assertTrue(should_continue)
        self.assertEqual(state["status"], "AUTH_TIMEOUT")
        self.assertEqual(state["auth"]["status"], "AUTH_TIMEOUT")

        with patch(
            "backend.app.demo.engine.authorize",
            new=AsyncMock(side_effect=AssertionError("Повторное ожидание недопустимо")),
        ):
            should_still_continue = await engine._ensure_authorized(FakePage())

        state = await STATE.snapshot()
        self.assertTrue(should_still_continue)
        self.assertEqual(state["auth"]["status"], "AUTH_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
