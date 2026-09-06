import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.browser.manager import BrowserManager
from backend.app.demo.engine import (
    BrowserStartError,
    DemoEngine,
    HistoryClearBlockedError,
    ModeConflictError,
)
from backend.app.demo.history import DemoRepository
from backend.app.demo.state import STATE


class FakeBrowserManager:
    def __init__(self):
        self.stop_calls = 0
        self.ensure_calls = 0
        self.generation = 0
        self.logger = None
        self.open = False
        self.page = FakePage(closed=True)
        self.start_error = None

    def set_logger(self, logger):
        self.logger = logger

    async def snapshot(self):
        status = "OPEN" if self.open and not self.page.is_closed() else "CLOSED"
        return {"status": status, "context": status, "page": status}

    async def ensure_page(self):
        self.ensure_calls += 1
        if self.start_error is not None:
            raise self.start_error
        if not self.open or self.page.is_closed():
            self.open = True
            self.generation += 1
            self.page = FakePage()
        return self.page

    def close_runtime(self):
        self.open = False
        self.page.closed = True

    async def stop(self):
        self.stop_calls += 1
        self.close_runtime()


class FakePage:
    def __init__(self, closed=False):
        self.closed = closed

    def is_closed(self):
        return self.closed


class StaleContext:
    def __init__(self):
        self.pages = []
        self.browser = None
        self.close_calls = 0

    async def new_page(self):
        raise RuntimeError("Target page, context or browser has been closed")

    async def close(self):
        self.close_calls += 1


class DemoLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_manager_discards_stale_context_instead_of_reusing_it(self):
        manager = BrowserManager()
        stale_context = StaleContext()
        replacement = FakePage()
        manager.context = stale_context
        manager.playwright = object()
        manager._context_closed = False

        async def launch_replacement(*, recovered):
            self.assertTrue(recovered)
            manager.generation += 1
            manager.page = replacement
            return replacement

        manager._launch_locked = launch_replacement

        page = await manager.ensure_page()

        self.assertIs(page, replacement)
        self.assertEqual(stale_context.close_calls, 1)
        self.assertEqual(manager.generation, 1)

    async def test_demo_stop_does_not_stop_browser(self):
        manager = FakeBrowserManager()
        engine = DemoEngine(manager)
        await manager.ensure_page()

        async def worker():
            await engine._stop_event.wait()

        engine._stop_event.clear()
        engine._task = asyncio.create_task(worker())
        state = await engine.stop()

        self.assertEqual(manager.stop_calls, 0)
        self.assertEqual(state["browser"]["status"], "OPEN")
        self.assertFalse(state["running"])

    async def test_start_creates_running_worker_and_opens_browser(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                state = await engine.start()

                self.assertTrue(state["running"])
                self.assertEqual(state["browser"]["status"], "OPEN")
                self.assertIsNotNone(engine.task)
                self.assertFalse(engine.task.done())
                await engine.stop()

    async def test_second_start_reuses_running_task(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                await engine.start()
                first_task = engine.task
                second_state = await engine.start()
                self.assertIs(engine.task, first_task)
                self.assertEqual(second_state["event"], "DEMO_ALREADY_RUNNING")
                self.assertEqual(manager.ensure_calls, 1)
                await engine.stop()

    async def test_live_double_start_keeps_one_worker_and_one_browser(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                first = await engine.start("LIVE")
                task = engine.task
                second = await engine.start("LIVE")

                self.assertTrue(first["running"])
                self.assertEqual(first["mode"], "LIVE")
                self.assertIs(engine.task, task)
                self.assertEqual(second["event"], "LIVE_ALREADY_RUNNING")
                self.assertEqual(manager.ensure_calls, 1)
                with self.assertRaises(ModeConflictError):
                    await engine.start("DEMO")
                await engine.stop("LIVE")

    async def test_browser_start_failure_never_reports_running(self):
        manager = FakeBrowserManager()
        manager.start_error = RuntimeError("chromium unavailable")
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                with self.assertRaises(BrowserStartError):
                    await engine.start()

                state = await STATE.snapshot()
                self.assertFalse(state["running"])
                self.assertEqual(state["event"], "BROWSER_START_FAILED")
                self.assertIsNone(engine.task)

    async def test_clear_history_is_blocked_while_worker_runs(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet({"id": "settled", "mode": "DEMO", "result": "WIN"})
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                await engine.start()

                with self.assertRaises(HistoryClearBlockedError):
                    await engine.clear_history()

                self.assertEqual(len(await repository.history(mode="DEMO")), 1)
                await engine.stop()

    async def test_clear_history_is_blocked_for_stopped_active_demo_bet(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet({"id": "active", "mode": "DEMO", "result": "ACTIVE"})
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                with self.assertRaises(HistoryClearBlockedError):
                    await engine.clear_history()

                self.assertIsNotNone(await repository.active_bet("DEMO"))

    async def test_stopped_worker_can_clear_demo_history(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet({"id": "settled", "mode": "DEMO", "result": "WIN"})
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                result = await engine.clear_history()

                self.assertTrue(result["ok"])
                self.assertEqual(result["deleted"], 1)
                self.assertEqual(result["items"], [])
                self.assertEqual(await repository.history(mode="DEMO"), [])

    async def test_stopped_worker_can_clear_entire_database(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet({"id": "settled", "mode": "DEMO", "result": "WIN"})
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                result = await engine.clear_database()

                self.assertTrue(result["ok"])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["state"]["status"], "STOPPED")
                self.assertEqual(result["state"]["bet"]["step"], 0)
                self.assertEqual(await repository.history(), [])
                self.assertEqual([item["event"] for item in await repository.logs()], ["DATABASE_CLEARED"])

    async def test_start_stop_start_stop_start_reuses_only_healthy_runtime(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                tasks = []
                states = []
                for cycle in range(3):
                    started = await engine.start()
                    tasks.append(engine.task)
                    states.append((started["running"], started["browser"]["status"]))
                    if cycle == 0:
                        manager.close_runtime()
                    if cycle < 2:
                        await engine.stop()

                self.assertEqual(states, [(True, "OPEN"), (True, "OPEN"), (True, "OPEN")])
                self.assertEqual(len({id(task) for task in tasks}), 3)
                self.assertEqual(manager.ensure_calls, 3)
                self.assertEqual(manager.generation, 2)
                await engine.stop()

    async def test_stale_live_recovery_does_not_block_demo_start(self):
        manager = FakeBrowserManager()
        with tempfile.TemporaryDirectory() as directory:
            repository = DemoRepository(Path(directory))
            await repository.save_bet(
                {"id": "old-live", "mode": "LIVE", "result": "ACTIVE", "status": "ACTIVE"}
            )
            await repository.save_sequence(status="RECOVERY_REQUIRED")
            with patch("backend.app.demo.engine.REPOSITORY", repository):
                engine = DemoEngine(manager)

                async def worker():
                    await engine._stop_event.wait()

                engine._run_guarded = worker
                state = await engine.start()

                self.assertTrue(state["running"])
                self.assertNotEqual(state["status"], "RECOVERING")
                self.assertEqual(state["sequence"]["status"], "WAITING_FOR_MATCH")
                self.assertFalse(engine.task.done())
                old_live = next(item for item in await repository.history() if item["id"] == "old-live")
                self.assertEqual(old_live["result"], "ACTIVE")
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
