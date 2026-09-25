import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

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
    COUPON_BET_SELECTOR,
    COUPON_FIRST_TEAM_SELECTOR,
    COUPON_MARKET_SELECTOR,
    COUPON_REMOVE_SELECTOR,
    EMPTY_COUPON_SELECTOR,
    COUPON_SECOND_TEAM_SELECTOR,
    CONFIRM_SELECTOR,
    CONFIRM_FALLBACK_SELECTOR,
    COUPON_SELECTOR,
    SUCCESS_MODAL_CONTINUE_SELECTOR,
    SUCCESS_MODAL_INFO_SELECTOR,
    SUCCESS_MODAL_SELECTOR,
    SUCCESS_MODAL_TITLE_SELECTOR,
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
        self.confirm_fallback = FakeLocator(count=0, visible=False)
        self.balance = FakeLocator(text="1000.00")
        self.reloads = 0
        self.success = FakeLocator(count=0, visible=False)
        self.failure = FakeLocator(count=0, visible=False)
        self.success_modal = FakeLocator(count=0, visible=False)
        self.success_title = FakeLocator(
            text="Ваша ставка принята!",
            count=0,
            visible=False,
        )
        self.success_info = FakeLocator(
            text="Купон № 87729189649",
            count=0,
            visible=False,
        )

        def close_success_modal():
            self.success_modal.present = 0
            self.success_modal.visible = False
            self.success_title.present = 0
            self.success_title.visible = False
            self.success_info.present = 0
            self.success_info.visible = False

        self.success_continue = FakeLocator(
            text="Продолжить",
            count=0,
            visible=False,
            on_click=close_success_modal,
        )
        self.success_modal.children = {
            SUCCESS_MODAL_TITLE_SELECTOR: self.success_title,
            SUCCESS_MODAL_INFO_SELECTOR: self.success_info,
            SUCCESS_MODAL_CONTINUE_SELECTOR: self.success_continue,
        }
        self.blocked_coupon = FakeLocator(count=0, visible=False)
        self.coupon_bet = FakeLocator()
        self.coupon_team1 = FakeLocator(text="TEAM 1")
        self.coupon_team2 = FakeLocator(text="TEAM 2")
        self.coupon_market = FakeLocator(text="Следующий гол: Команда 2 - 4-й гол")
        self.coupon_remove = FakeLocator(count=0, visible=False)
        self.empty_coupon = FakeLocator(count=0, visible=False)
        self.coupon_bet.children = {
            COUPON_FIRST_TEAM_SELECTOR: self.coupon_team1,
            COUPON_SECOND_TEAM_SELECTOR: self.coupon_team2,
            COUPON_MARKET_SELECTOR: self.coupon_market,
            COUPON_REMOVE_SELECTOR: self.coupon_remove,
        }
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
            COUPON_BET_SELECTOR: self.coupon_bet,
            EMPTY_COUPON_SELECTOR: self.empty_coupon,
            ACCOUNT_SELECTOR: self.account,
            AMOUNT_SELECTOR: self.amount,
            CONFIRM_SELECTOR: self.confirm,
            CONFIRM_FALLBACK_SELECTOR: self.confirm_fallback,
            BALANCE_SELECTOR: self.balance,
            BLOCKED_COUPON_SELECTOR: self.blocked_coupon,
            BLOCKED_TEXT_SELECTOR: self.blocked_text,
            BLOCKED_REMOVE_SELECTOR: self.blocked_remove,
            SUCCESS_MODAL_SELECTOR: self.success_modal,
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

    def show_success_modal(self):
        self.success_modal.present = 1
        self.success_modal.visible = True
        self.success_title.present = 1
        self.success_title.visible = True
        self.success_info.present = 1
        self.success_info.visible = True
        self.success_continue.present = 1
        self.success_continue.visible = True

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
                "LIVE_COUPON_SELECTION_VERIFIED",
                "LIVE_AMOUNT_FILLED",
                "LIVE_AMOUNT_VERIFIED",
                "LIVE_BALANCE_BEFORE",
                "TEST_AUTO_CONFIRM",
                "LIVE_COUPON_CLICK_ATTEMPT",
                "LIVE_COUPON_CLICKED",
                "AWAITING_PLACEMENT_RESULT",
            ],
        )

    async def test_confirm_accepts_uppercase_label(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.confirm = FakeLocator(text="СДЕЛАТЬ СТАВКУ")

        await executor.prepare(page, item)

        self.assertEqual(page.confirm.clicks, 1)

    async def test_confirm_waits_for_coupon_button_to_be_enabled(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.confirm.disabled = True

        async def enable():
            await asyncio.sleep(0.02)
            page.confirm.disabled = False

        task = asyncio.create_task(enable())
        await executor.prepare(page, item)
        await task

        self.assertEqual(page.confirm.clicks, 1)

    async def test_confirm_not_found_records_coupon_button_diagnostics(self):
        events = []

        async def log(event, message):
            events.append((event, message))

        executor, page, item = LiveExecutor(log), FakePage(), decision()
        page.confirm.present = 0
        page.confirm_fallback = FakeLocator(text="Проверить ставку")

        with patch("backend.app.live.executor.CONFIRM_WAIT_SECONDS", 0.02):
            with self.assertRaises(LivePreparationError) as raised:
                await executor.prepare(page, item)

        self.assertEqual(raised.exception.status, "LIVE_CONFIRM_BUTTON_NOT_FOUND")
        self.assertEqual(page.confirm.clicks, 0)
        self.assertTrue(any(
            event == "LIVE_CONFIRM_BUTTON_DIAGNOSTICS" and "Проверить ставку" in message
            for event, message in events
        ))

    async def test_disabled_confirm_never_submits(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.confirm.disabled = True

        with patch("backend.app.live.executor.CONFIRM_WAIT_SECONDS", 0.02):
            with self.assertRaises(LivePreparationError) as raised:
                await executor.prepare(page, item)

        self.assertEqual(raised.exception.status, "LIVE_CONFIRM_BUTTON_NOT_READY")
        self.assertEqual(page.confirm.clicks, 0)

    async def test_coupon_disappears_after_amount_fill_without_submission(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        original_fill = page.amount.fill

        async def fill(value):
            await original_fill(value)
            if value:
                page.empty_coupon.present = 1
                page.empty_coupon.visible = True
                page.coupon_bet.present = 0
                page.confirm.present = 0

        page.amount.fill = fill

        with self.assertRaises(LivePreparationError) as raised:
            await executor.prepare(page, item)

        self.assertEqual(raised.exception.status, "LIVE_COUPON_LOST_BEFORE_CONFIRM")
        self.assertEqual(page.confirm.clicks, 0)

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

    async def test_success_modal_confirms_bet_and_clicks_continue(self):
        events = []

        async def log(event, message):
            events.append((event, message))

        executor, page, item = LiveExecutor(log), FakePage(), decision(amount=10)
        await executor.prepare(page, item)
        page.show_success_modal()

        observation = await executor.wait_for_manual_confirmation(
            page,
            item,
            asyncio.Event(),
        )

        self.assertTrue(observation.placed)
        self.assertIn("coupon_id=87729189649", observation.signal)
        self.assertEqual(page.success_continue.clicks, 1)
        self.assertFalse(page.success_modal.visible)
        self.assertEqual(page.reloads, 0)
        self.assertEqual(executor.state(item.attempt_id), LiveStatus.ACTIVE)
        self.assertIn("LIVE_SUCCESS_MODAL_DETECTED", [event for event, _ in events])
        self.assertIn(
            "LIVE_SUCCESS_MODAL_CONTINUE_CLICKED",
            [event for event, _ in events],
        )

    async def test_confirmed_bet_clears_lingering_coupon_with_remove_button(self):
        executor, page, item = LiveExecutor(), FakePage(), decision(amount=42)
        page.coupon_remove.present = 1
        page.coupon_remove.visible = True
        await executor.prepare(page, item)
        page.balance.text = "958.00"

        observation = await executor.wait_for_manual_confirmation(
            page, item, asyncio.Event()
        )
        self.assertTrue(observation.placed)
        self.assertEqual(page.coupon_remove.clicks, 1)

    async def test_missing_balance_debit_resolves_not_accepted_without_second_click(self):
        executor, page, item = LiveExecutor(), FakePage(), decision(amount=42)
        await executor.prepare(page, item)

        with patch(
            "backend.app.live.executor.SUBMISSION_RECONCILE_SECONDS",
            0.02,
        ):
            observation = await executor.wait_for_manual_confirmation(
                page, item, asyncio.Event()
            )

        self.assertIsNotNone(observation)
        self.assertFalse(observation.placed)
        self.assertTrue(observation.retryable)
        self.assertEqual(observation.signal, "NOT_ACCEPTED_NO_CONFIRMATION")
        self.assertEqual(page.reloads, 0)
        self.assertEqual(page.confirm.clicks, 1)

    async def test_ambiguous_confirm_click_is_reconciled_without_second_click(self):
        events = []

        async def log(event, message):
            events.append((event, message))

        executor, page, item = LiveExecutor(log), FakePage(), decision(amount=42)

        async def unstable_click(**_kwargs):
            page.confirm.clicks += 1
            raise TimeoutError("element is not stable")

        page.confirm.click = unstable_click

        await executor.prepare(page, item)

        self.assertEqual(page.confirm.clicks, 1)
        self.assertTrue(await executor.manual_click_seen(item.attempt_id))
        self.assertEqual(
            executor.state(item.attempt_id),
            LiveStatus.AWAITING_PLACEMENT_RESULT,
        )
        self.assertIn(
            "LIVE_COUPON_BUTTON_UNSTABLE",
            [event for event, _ in events],
        )

    async def test_clear_unaccepted_regular_coupon_is_idempotent(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.coupon_remove.present = 1
        page.coupon_remove.visible = True

        def remove_coupon():
            page.coupon_bet.present = 0
            page.coupon_bet.visible = False
            page.coupon_remove.present = 0
            page.coupon_remove.visible = False
            page.amount.present = 0
            page.amount.visible = False
            page.confirm.present = 0
            page.confirm.visible = False

        page.coupon_remove.on_click = remove_coupon

        first = await executor.clear_unaccepted_coupon(page, item.attempt_id)
        second = await executor.clear_unaccepted_coupon(page, item.attempt_id)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(page.coupon_remove.clicks, 1)

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

    async def test_wrong_coupon_team_or_goal_never_submits(self):
        for name, market in (
            ("OTHER TEAM", "Следующий гол: Команда 2 - 4-й гол"),
            ("TEAM 2", "Следующий гол: Команда 2 - 5-й гол"),
            ("TEAM 2", "Следующий гол: Команда 1 - 4-й гол"),
        ):
            with self.subTest(name=name, market=market):
                executor, page, item = LiveExecutor(), FakePage(), decision()
                page.coupon_team2.text = name
                page.coupon_market.text = market
                with self.assertRaises(LivePreparationError) as raised:
                    await executor.prepare(page, item)
                self.assertEqual(raised.exception.status, "LIVE_COUPON_SELECTION_MISMATCH")
                self.assertEqual(page.confirm.clicks, 0)

    async def test_multiple_coupon_bets_never_submit(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.coupon_bet.present = 2
        with self.assertRaises(LivePreparationError) as raised:
            await executor.prepare(page, item)
        self.assertEqual(raised.exception.status, "LIVE_COUPON_SELECTION_UNKNOWN")
        self.assertEqual(page.confirm.clicks, 0)

    async def test_waits_for_coupon_selection_card_after_amount_input(self):
        events = []

        async def log(event, _message):
            events.append(event)

        executor, page, item = LiveExecutor(log), FakePage(), decision()
        page.coupon_bet.present = 0

        async def render_selection():
            await asyncio.sleep(0.02)
            page.coupon_bet.present = 1

        render_task = asyncio.create_task(render_selection())
        signal = await executor._wait_for_coupon_selection(
            page,
            item,
            timeout_seconds=0.20,
        )
        await render_task

        self.assertEqual(signal, "VERIFIED")
        self.assertIn("LIVE_COUPON_SELECTION_RENDER_WAIT", events)
        self.assertIn("LIVE_COUPON_SELECTION_RENDERED", events)

    async def test_missing_coupon_selection_is_definitely_not_submitted(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.coupon_bet.present = 0

        with self.assertRaises(LivePreparationError) as raised:
            await executor._wait_for_coupon_selection(
                page,
                item,
                timeout_seconds=0.02,
            )

        self.assertEqual(
            raised.exception.status,
            "LIVE_COUPON_SELECTION_NOT_RENDERED",
        )
        self.assertEqual(page.amount.fills, [])
        self.assertEqual(page.confirm.clicks, 0)

    async def test_visible_empty_coupon_does_not_count_as_selected_bet(self):
        executor, page, item = LiveExecutor(), FakePage(), decision()
        page.empty_coupon.present = 1
        page.empty_coupon.visible = True
        page.amount.present = 0
        page.confirm.present = 0

        signal = await executor._wait_for_coupon_surface(page, timeout_seconds=0.02)
        self.assertEqual(signal, "EMPTY_COUPON")
        with patch.object(executor, "_wait_for_coupon_surface", return_value=signal):
            with self.assertRaises(LivePreparationError) as raised:
                await executor.prepare(page, item)
        self.assertEqual(raised.exception.status, "LIVE_COUPON_EMPTY_AFTER_CLICK")
        self.assertEqual(page.confirm.clicks, 0)

    async def test_first_half_draw_shared_executor_is_unchanged(self):
        executor, page = LiveExecutor(), FakePage()
        item = replace(decision(), team="Ничья", side=Scorer.UNKNOWN, goal_number=0)
        page.coupon_bet.present = 0
        await executor.prepare(page, item)
        self.assertEqual(page.confirm.clicks, 1)

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
