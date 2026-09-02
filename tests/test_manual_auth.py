import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from auth import (
    AUTHORIZED_CONTINUE_DELAY,
    BALANCE_SELECTOR,
    SITE_URL,
    authorize,
    check_balance,
)


class FakeTextElement:
    def __init__(self, text):
        self.text = text

    async def text_content(self):
        return self.text


class FakeCurrencyLocator:
    def __init__(self, texts):
        self.texts = texts

    async def count(self):
        return len(self.texts)

    def nth(self, index):
        return FakeTextElement(self.texts[index])


class FakePage:
    def __init__(self, balance_checks):
        self.balance_checks = list(balance_checks)
        self.calls = []

    def locator(self, selector):
        self.calls.append(("locator", selector))
        texts = self.balance_checks.pop(0) if self.balance_checks else []
        return FakeCurrencyLocator(texts)

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", url, kwargs))

    async def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))

    def is_closed(self):
        return False


class ManualAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_balance_currency_with_exact_rub_is_authorized(self):
        authorized = await check_balance(FakePage([["USD", " RUB "]]))
        wrong_case = await check_balance(FakePage([["rub"]]))

        self.assertTrue(authorized)
        self.assertFalse(wrong_case)

    async def test_saved_session_waits_25_seconds_before_continuing(self):
        page = FakePage([["RUB"]])
        statuses = []

        async def on_waiting():
            statuses.append("WAITING_MANUAL_LOGIN")

        async def on_authorized():
            statuses.append("AUTHORIZED")

        wait_mock = AsyncMock(return_value=True)
        with patch("auth._wait_poll_interval", wait_mock):
            result = await authorize(
                page,
                on_waiting=on_waiting,
                on_authorized=on_authorized,
            )

        self.assertEqual(result, {"ok": True, "status": "AUTHORIZED"})
        self.assertEqual(statuses, ["AUTHORIZED"])
        wait_mock.assert_awaited_once_with(None, AUTHORIZED_CONTINUE_DELAY)
        self.assertEqual(page.calls[0][0], "goto")
        self.assertEqual(page.calls[0][1], SITE_URL)

    async def test_manual_login_continues_without_extra_25_seconds(self):
        page = FakePage([[], ["RUB"]])
        statuses = []

        async def on_waiting():
            statuses.append("WAITING_MANUAL_LOGIN")

        async def on_authorized():
            statuses.append("AUTHORIZED")

        wait_mock = AsyncMock(return_value=True)
        with patch("auth._wait_poll_interval", wait_mock):
            result = await authorize(
                page,
                asyncio.Event(),
                on_waiting,
                on_authorized,
            )

        self.assertEqual(
            statuses,
            ["WAITING_MANUAL_LOGIN", "AUTHORIZED"],
        )
        self.assertEqual(result, {"ok": True, "status": "AUTHORIZED"})
        wait_mock.assert_not_awaited()

    async def test_timeout_continues_the_flow(self):
        page = FakePage([[]])

        with patch("auth.MANUAL_LOGIN_TIMEOUT", 0.0):
            result = await authorize(page, asyncio.Event())

        self.assertEqual(result, {"ok": True, "status": "AUTH_TIMEOUT"})

    async def test_stop_event_interrupts_manual_login_wait(self):
        page = FakePage([[]])
        stop_event = asyncio.Event()
        stop_event.set()

        result = await authorize(page, stop_event)

        self.assertEqual(result, {"ok": False, "status": "AUTH_STOPPED"})

    async def test_authorization_never_controls_the_page(self):
        page = FakePage([["RUB"]])
        with patch("auth._wait_poll_interval", AsyncMock(return_value=True)):
            await authorize(page)

        action_names = [call[0] for call in page.calls]
        self.assertNotIn("reload", action_names)
        self.assertNotIn("click", action_names)
        self.assertNotIn("fill", action_names)
        self.assertEqual(
            [call[1] for call in page.calls if call[0] == "locator"],
            [BALANCE_SELECTOR],
        )


if __name__ == "__main__":
    unittest.main()
