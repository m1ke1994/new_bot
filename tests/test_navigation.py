import unittest

from backend.app.browser.navigation import NavigationLoadError, goto_with_retry


class FakeNavigationPage:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.url = "about:blank"
        self.goto_calls = 0
        self.stop_calls = 0
        self.wait_calls = []
        self.ready_state = "complete"
        self.has_body = True

    def is_closed(self):
        return False

    async def goto(self, url, *, wait_until, timeout):
        self.goto_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.url = outcome
        return object()

    async def evaluate(self, expression):
        if "window.stop" in expression and "readyState" not in expression:
            self.stop_calls += 1
            return None
        return {"readyState": self.ready_state, "hasBody": self.has_body}

    async def wait_for_timeout(self, timeout):
        self.wait_calls.append(timeout)


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_interrupted_navigation_and_verifies_dom(self):
        page = FakeNavigationPage(
            [RuntimeError("navigation interrupted"), "https://example.test/ru/live"]
        )

        await goto_with_retry(
            page,
            "https://example.test/ru",
            attempts=2,
            retry_delay_ms=10,
        )

        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(page.stop_calls, 1)
        self.assertEqual(page.wait_calls, [10])

    async def test_timeout_is_accepted_when_target_dom_is_already_ready(self):
        page = FakeNavigationPage([RuntimeError("timeout")])
        page.url = "https://example.test/ru"

        await goto_with_retry(page, "https://example.test/ru", attempts=2)

        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.stop_calls, 0)

    async def test_raises_after_all_attempts_fail(self):
        page = FakeNavigationPage([RuntimeError("first"), RuntimeError("second")])

        with self.assertRaises(NavigationLoadError):
            await goto_with_retry(page, "https://example.test/ru", attempts=2)

        self.assertEqual(page.goto_calls, 2)

