import asyncio
import unittest

from backend.app.demo.models import Scorer
from backend.app.live.executor import (
    ACCOUNT_SELECTOR,
    AMOUNT_SELECTOR,
    BALANCE_SELECTOR,
    BLOCKED_COUPON_SELECTOR,
    BLOCKED_EVENT_SIGNAL,
    BLOCKED_REMOVE_FALLBACK_SELECTOR,
    BLOCKED_REMOVE_SELECTOR,
    BLOCKED_TEXT_SELECTOR,
    CONFIRM_SELECTOR,
    COUPON_SELECTOR,
    LiveExecutor,
)
from backend.app.live.models import LiveDecision, LivePreparationError, LiveStatus


class FakeLocator:
    def __init__(self, *, text="", value="", count=1, visible=True, disabled=False, keep_value=False, on_click=None):
        self.text = text
        self.value = value
        self.present = count
        self.visible = visible
        self.disabled = disabled
        self.keep_value = keep_value
        self.attributes = {}
        self.clicks = 0
        self.fills = []
        self.on_click = on_click
        self.children = {}

    @property
    def first(self):
        return self

    def nth(self, _index):
        return self

    def locator(self, selector):
        return self.children.get(selector, FakeLocator(count=0, visible=False))

    async def count(self):
        return self.present

    async def is_visible(self):
        return self.visible

    async def is_disabled(self):
        return self.disabled

    async def wait_for(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.clicks += 1
        if "data-autobet-manual-click" in self.attributes:
            self.attributes["data-autobet-manual-click"] = "1"
        if self.on_click is not None:
            self.on_click()

    async def fill(self, value):
        self.fills.append(value)
        if not self.keep_value:
            self.value = value

    async def input_value(self):
        return self.value

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def element_handle(self):
        return self

    async def evaluate(self, _script):
        self.attributes["data-autobet-manual-click"] = "0"


class FakePage:
    def __init__(self):
        self.coupon = FakeLocator()
        self.account = FakeLocator(text="Основной (RUB)")
        self.amount = FakeLocator()
        self.confirm = FakeLocator(text="Сделать ставку")
        self.balance = FakeLocator(text="1000.00")
        self.reloads = 0
        self.success = FakeLocator(count=0, visible=False)
        self.failure = FakeLocator(count=0, visible=False)
        self.blocked_coupon = FakeLocator(count=0, visible=False)
        self.blocked_text = FakeLocator(
            text="Заблокированное событие",
            count=0,
            visible=False,
        )

        def remove_blocked():
            self.blocked_coupon.present = 0
            self.blocked_coupon.visible = False
            self.blocked_text.present = 0
            self.blocked_text.visible = False

        self.blocked_remove = FakeLocator(
            count=0,
            visible=False,
            on_click=remove_blocked,
        )
        self.blocked_coupon.children = {
            BLOCKED_TEXT_SELECTOR: self.blocked_text,
            BLOCKED_REMOVE_SELECTOR: self.blocked_remove,
            BLOCKED_REMOVE_FALLBACK_SELECTOR: self.blocked_remove,
        }

    def locator(self, selector):
        return {
            COUPON_SELECTOR: self.coupon,
            ACCOUNT_SELECTOR: self.account,
            AMOUNT_SELECTOR: self.amount,
            CONFIRM_SELECTOR: self.confirm,
            BALANCE_SELECTOR: self.balance,
            BLOCKED_COUPON_SELECTOR: self.blocked_coupon,
            BLOCKED_TEXT_SELECTOR: self.blocked_text,
            BLOCKED_REMOVE_SELECTOR: self.blocked_remove,
        }[selector]

    async def reload(self, **_kwargs):
        self.reloads += 1
        return None

    def show_blocked_event(self):
        self.blocked_coupon.present = 1
        self.blocked_coupon.visible = True
        self.blocked_text.present = 1
        self.blocked_text.visible = True
        self.blocked_remove.present = 1
        self.blocked_remove.visible = True

    def get_by_text(self, pattern):
        return self.failure if "не принята" in pattern.pattern else self.success


def decision(locator=None, *, amount=42):
    return LiveDecision(
        attempt_id="match_TEAM_2_step2_goal4_attempt1",
        match_id="match",
        team="TEAM 2",
        side=Scorer.TEAM_2,
        strategy_step=2,
        amount=amount,
        goal_number=4,
        coefficient=2.03,
        coefficient_locator=locator or FakeLocator(text="2.03"),
    )


class LiveExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_blocked_event_returns_retryable_not_placed(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        await executor.prepare(page, item)
        page.show_blocked_event()

        observation = await executor.wait_for_manual_confirmation(
            page,
            item,
            asyncio.Event(),
        )

        self.assertFalse(observation.placed)
        self.assertTrue(observation.retryable)
        self.assertEqual(observation.signal, BLOCKED_EVENT_SIGNAL)
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.AWAITING_PLACEMENT_RESULT)

    async def test_blocked_coupon_removal_is_idempotent(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.show_blocked_event()

        first = await executor.remove_blocked_coupon(page, item.attempt_id)
        second = await executor.remove_blocked_coupon(page, item.attempt_id)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(page.blocked_remove.clicks, 1)
        self.assertFalse(await executor.blocked_event_exists(page))

    async def test_blocked_signal_requires_exact_text_inside_visible_container(self):
        executor, page = LiveExecutor(), FakePage()
        page.blocked_text.present = 1
        page.blocked_text.visible = True

        self.assertFalse(await executor.blocked_event_exists(page))

        page.show_blocked_event()
        page.blocked_text.text = "Заблокированное событие временно"
        self.assertFalse(await executor.blocked_event_exists(page))

        page.blocked_text.text = "  Заблокированное   событие  "
        self.assertTrue(await executor.blocked_event_exists(page))

    async def test_current_coupon_dom_does_not_require_legacy_root(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.coupon.present = 0
        page.coupon.visible = False

        await executor.prepare(page, item)

        self.assertEqual(page.amount.fills, ["", "42"])
        self.assertEqual(page.confirm.clicks, 1)
        self.assertEqual(
            executor.state(item.attempt_id),
            LiveStatus.AWAITING_PLACEMENT_RESULT,
        )

    async def test_prepares_coupon_and_uses_existing_auto_confirm_mode(self):
        events = []

        async def log(event, _message):
            events.append(event)

        executor, page = LiveExecutor(log), FakePage()
        market = FakeLocator(text="Команда 2 - 4-й гол")
        item = decision(market)

        await executor.prepare(page, item)

        self.assertEqual(market.clicks, 1)
        self.assertEqual(page.amount.fills, ["", "42"])
        self.assertEqual(await page.amount.input_value(), "42")
        self.assertEqual(page.confirm.clicks, 1)
        self.assertTrue(executor.market_was_selected(item.attempt_id))
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.AWAITING_PLACEMENT_RESULT)
        self.assertEqual(
            events,
            [
                "LIVE_MARKET_SELECTED",
                "LIVE_COUPON_OPENED",
                "LIVE_AMOUNT_FILLED",
                "LIVE_AMOUNT_VERIFIED",
                "LIVE_BALANCE_BEFORE",
                "TEST_AUTO_CONFIRM",
                "AWAITING_PLACEMENT_RESULT",
            ],
        )

    async def test_auto_click_balance_debit_activates_bet(self):
        executor, page, item = LiveExecutor(), FakePage(), decision(amount=42)
        await executor.prepare(page, item)
        page.balance.text = "958.00"

        observation = await executor.wait_for_manual_confirmation(
            page, item, asyncio.Event()
        )

        self.assertTrue(observation.placed)
        self.assertEqual(observation.signal, "balance debit confirmed")
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.ACTIVE)
        self.assertEqual(page.confirm.clicks, 1)

    async def test_missing_balance_debit_reloads_once_and_stays_unknown(self):
        executor, page, item = LiveExecutor(), FakePage(), decision(amount=42)
        await executor.prepare(page, item)

        observation = await executor.wait_for_manual_confirmation(
            page, item, asyncio.Event()
        )

        self.assertIsNone(observation)
        self.assertEqual(page.reloads, 1)
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.AWAITING_PLACEMENT_RESULT)
        self.assertEqual(page.confirm.clicks, 1)

    async def test_blocked_coupon_after_market_click_skips_amount_and_confirm(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        original_click = item.coefficient_locator.on_click

        def show_lock():
            if original_click is not None:
                original_click()
            page.show_blocked_event()

        item.coefficient_locator.on_click = show_lock
        await executor.prepare(page, item)

        self.assertEqual(page.amount.fills, [])
        self.assertEqual(page.confirm.clicks, 0)
        observation = await executor.wait_for_manual_confirmation(
            page, item, asyncio.Event()
        )
        self.assertFalse(observation.placed)
        self.assertEqual(observation.signal, BLOCKED_EVENT_SIGNAL)

    async def test_success_text_without_manual_click_is_not_accepted(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        await executor.prepare(page, item)
        page.success.present = 1
        page.success.visible = True
        stop = asyncio.Event()
        stop.set()

        observation = await executor.wait_for_manual_confirmation(page, item, stop)

        self.assertIsNone(observation)
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.AWAITING_PLACEMENT_RESULT)

    async def test_wrong_amount_is_rejected(self):
        executor, page, item = LiveExecutor(), FakePage(), decision(amount=94)
        page.amount.value = "42"
        page.amount.keep_value = True

        with self.assertRaises(LivePreparationError) as raised:
            await executor.prepare(page, item)

        self.assertEqual(raised.exception.status, "LIVE_AMOUNT_VERIFICATION_FAILED")
        self.assertEqual(page.confirm.clicks, 0)
        self.assertTrue(executor.market_was_selected(item.attempt_id))

    async def test_failed_market_click_does_not_claim_coupon_was_opened(self):
        class FailedMarket(FakeLocator):
            async def click(self):
                raise RuntimeError("locked")

        executor, page = LiveExecutor(), FakePage()
        item = decision(FailedMarket())

        with self.assertRaises(LivePreparationError):
            await executor.prepare(page, item)

        self.assertFalse(executor.market_was_selected(item.attempt_id))

    async def test_duplicate_preparation_does_not_open_coupon_twice(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        await executor.prepare(page, item)

        with self.assertRaises(LivePreparationError) as raised:
            await executor.prepare(page, item)

        self.assertEqual(raised.exception.status, "DUPLICATE_LIVE_PREPARATION")
        self.assertEqual(item.coefficient_locator.clicks, 1)


if __name__ == "__main__":
    unittest.main()
