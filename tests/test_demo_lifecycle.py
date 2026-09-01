import asyncio
import unittest

from backend.app.demo.engine import DemoEngine


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


if __name__ == "__main__":
    unittest.main()
