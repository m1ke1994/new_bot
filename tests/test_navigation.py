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
        self.evaluate_failures = 0
        self.document_href = None
        self.document_title = ""
        self.body_text = ""

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
        if self.evaluate_failures > 0:
            self.evaluate_failures -= 1
            raise RuntimeError("execution context was destroyed")
        return {
            "readyState": self.ready_state,
            "hasBody": self.has_body,
            "href": self.document_href or self.url,
            "title": self.document_title,
            "bodyText": self.body_text,
        }

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

    async def test_aborted_redirect_is_accepted_after_replacement_frame_attaches(self):
        page = FakeNavigationPage(
            [RuntimeError("net::ERR_ABORTED; maybe frame was detached")]
        )
        page.url = "https://example.test/ru"
        page.evaluate_failures = 1

        await goto_with_retry(page, "https://example.test/ru", attempts=1)

        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.stop_calls, 0)

    async def test_network_changed_error_page_is_not_accepted_as_ready_dom(self):
        page = FakeNavigationPage(
            ["https://example.test/ru", "https://example.test/ru/live"]
        )
        page.body_text = "Соединение прервано ERR_NETWORK_CHANGED"

        original_goto = page.goto

        async def goto_and_recover(url, *, wait_until, timeout):
            result = await original_goto(url, wait_until=wait_until, timeout=timeout)
            if page.goto_calls >= 2:
                page.body_text = ""
            return result

        page.goto = goto_and_recover

        await goto_with_retry(
            page,
            "https://example.test/ru",
            attempts=2,
            retry_delay_ms=10,
        )

        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(page.stop_calls, 1)

    async def test_chrome_error_document_is_rejected(self):
        page = FakeNavigationPage(
            ["https://example.test/ru", "https://example.test/ru"]
        )
        page.document_href = "chrome-error://chromewebdata/"

        original_goto = page.goto

        async def goto_and_recover(url, *, wait_until, timeout):
            result = await original_goto(url, wait_until=wait_until, timeout=timeout)
            if page.goto_calls >= 2:
                page.document_href = url
            return result

        page.goto = goto_and_recover

        await goto_with_retry(
            page,
            "https://example.test/ru",
            attempts=2,
            retry_delay_ms=10,
        )

        self.assertEqual(page.goto_calls, 2)

    async def test_raises_after_all_attempts_fail(self):
        page = FakeNavigationPage([RuntimeError("first"), RuntimeError("second")])

        with self.assertRaises(NavigationLoadError):
            await goto_with_retry(page, "https://example.test/ru", attempts=2)

        self.assertEqual(page.goto_calls, 2)

    async def test_final_error_contains_page_diagnostics(self):
        page = FakeNavigationPage([RuntimeError("aborted")])

        with self.assertRaises(NavigationLoadError) as caught:
            await goto_with_retry(page, "https://example.test/ru", attempts=1)

        self.assertIn("current_url='about:blank'", str(caught.exception))
        self.assertIn("page_closed=False", str(caught.exception))
