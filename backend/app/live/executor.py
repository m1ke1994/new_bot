import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import LiveDecision, LivePreparationError, LiveStatus, PlacementObservation


Logger = Callable[[str, str], Awaitable[Any]]
Publisher = Callable[[LiveStatus, str], Awaitable[Any]]

# Legacy root selectors are kept only as compatibility fallbacks.
COUPON_SELECTOR = ".coupon-bets, .quick-coupon-main"
COUPON_BET_SELECTOR = ".coupon-app .coupon-bets__bet"
EMPTY_COUPON_SELECTOR = ".coupon-app .coupon-main-tab--no-bets"
COUPON_FIRST_TEAM_SELECTOR = '[data-test="betting-coupon-bet-first-team"]'
COUPON_SECOND_TEAM_SELECTOR = '[data-test="betting-coupon-bet-second-team"]'
COUPON_MARKET_SELECTOR = '[data-test="betting-coupon-bet-market-name"]'
COUPON_REMOVE_SELECTOR = 'button.coupon-bet-header__remove[aria-label="Удалить"]'
# Legacy account selector is kept only as an optional compatibility check.
ACCOUNT_SELECTOR = '.quick-coupon-main button[aria-label="Основной (RUB)"]'
AMOUNT_SELECTOR = (
    '.coupon-app .coupon-amount input.ui-number-input__field[type="text"][inputmode="decimal"], '
    ".coupon-app .coupon-amount input.ui-number-input__field, "
    '.quick-coupon-main input.ui-number-input__field[placeholder="Введите сумму ставки"]'
)
CONFIRM_BUTTON_CLASS_SELECTOR = (
    "button.ui-button.ui-button--size-m.ui-button--theme-accent"
    ".ui-button--block.ui-button--uppercase.ui-button--rounded"
)
CONFIRM_FALLBACK_SELECTOR = (
    ".coupon-app .coupon-main-tab__make-bet .coupon-buttons button, "
    ".coupon-app .coupon-buttons button"
)
CONFIRM_SELECTOR = (
    f"{CONFIRM_FALLBACK_SELECTOR}, "
    f"{CONFIRM_BUTTON_CLASS_SELECTOR}, "
    f".coupon-buttons {CONFIRM_BUTTON_CLASS_SELECTOR}, "
    f".coupon-app {CONFIRM_BUTTON_CLASS_SELECTOR}, "
    ".quick-coupon-main button.quick-coupon-put-bet-button"
)
CONFIRM_TEXT = "Сделать ставку"
CONFIRM_WAIT_SECONDS = 5.0
CONFIRM_CLICK_TIMEOUT_MS = 1_250
CONFIRM_PRE_DISPATCH_RETRIES = 3
CONFIRM_PRE_DISPATCH_RETRY_DELAY_SECONDS = 0.08
SUBMISSION_RECONCILE_SECONDS = 2.5
COUPON_SELECTION_WAIT_SECONDS = 10.0
BALANCE_SELECTOR = '[data-gtm="account-balance-value-desktop"]'
BLOCKED_COUPON_SELECTOR = (
    ".coupon-bet__lock, "
    ".quick-coupon-events-card__lock"
)
BLOCKED_TEXT_SELECTOR = (
    ".coupon-bet-lock__text, .quick-coupon-events-card-lock__text"
)
BLOCKED_REMOVE_SELECTOR = (
    ".coupon-bet-lock-remove, .quick-coupon-events-card-lock__remove"
)
BLOCKED_REMOVE_FALLBACK_SELECTOR = 'button[aria-label="Удалить"]'
BLOCKED_TEXT = "Заблокированное событие"
BLOCKED_EVENT_SIGNAL = "BLOCKED_EVENT"
BALANCE_TOLERANCE = Decimal("0.01")
SUCCESS_MODAL_SELECTOR = '.modal__content[data-test="modal-content"]'
SUCCESS_MODAL_TITLE_SELECTOR = '[data-test="betting-coupon-success-modal-title"]'
SUCCESS_MODAL_INFO_SELECTOR = '[data-test="betting-coupon-success-modal-info"]'
SUCCESS_MODAL_CONTINUE_SELECTOR = (
    'button[data-test="betting-coupon-modal-control-contune"]'
)
SUCCESS_MODAL_CLOSE_SELECTOR = (
    'button[data-test="modal-control"][data-original-title="Закрыть"], '
    'button[data-test="modal-control"]'
)
SUCCESS_MODAL_TITLE = "Ваша ставка принята!"
SUCCESS_MODAL_WAIT_SECONDS = 2.0
SUCCESS_MODAL_BALANCE_RACE_SECONDS = 1.5
ACCEPTED_COUPON_CLEANUP_SECONDS = 0.35

# ТЕСТОВЫЙ АККАУНТ:
# автоподтверждение включено прямо в коде, ENV больше не требуется.
# Перед использованием другого аккаунта переключите значение на False.
TEST_AUTO_CONFIRM = True


def _amount_text(amount: float) -> str:
    decimal = Decimal(str(amount))
    return str(int(decimal)) if decimal == decimal.to_integral_value() else format(decimal, "f")


def _parse_amount(value: str) -> Decimal:
    normalized = value.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation as error:
        raise LivePreparationError(
            "LIVE_AMOUNT_VERIFICATION_FAILED", f"Некорректное значение суммы: {value!r}"
        ) from error


class LiveExecutor:
    """The only component allowed to prepare a real bookmaker coupon."""

    def __init__(self, logger: Logger | None = None) -> None:
        self._logger = logger
        self._states: dict[str, LiveStatus] = {}
        self._confirm_handles: dict[str, Any] = {}
        self._market_selected: set[str] = set()
        self._submission_attempted: set[str] = set()
        self._recovered_blocked_attempts: set[str] = set()
        self._balance_before: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()

    async def _log(self, event: str, message: str) -> None:
        if self._logger is not None:
            await self._logger(event, message)

    async def _publish(
        self,
        attempt_id: str,
        status: LiveStatus,
        message: str,
        publish: Publisher | None,
    ) -> None:
        self._states[attempt_id] = status
        await self._log(status.value, message)
        if publish is not None:
            await publish(status, message)

    def state(self, attempt_id: str) -> LiveStatus:
        return self._states.get(attempt_id, LiveStatus.IDLE)

    def market_was_selected(self, attempt_id: str) -> bool:
        return attempt_id in self._market_selected

    async def manual_click_seen(self, attempt_id: str) -> bool:
        if attempt_id in self._submission_attempted:
            return True
        handle = self._confirm_handles.get(attempt_id)
        if handle is None:
            return False
        try:
            return await handle.get_attribute("data-autobet-manual-click") == "1"
        except Exception:
            return attempt_id in self._submission_attempted
    async def _read_balance(self, page: Any) -> Decimal:
        balance = page.locator(BALANCE_SELECTOR).first
        if await balance.count() == 0 or not await balance.is_visible():
            raise LivePreparationError(
                "LIVE_BALANCE_NOT_FOUND",
                "Баланс RUB не найден в header.",
            )
        raw = await balance.inner_text()
        try:
            return _parse_amount(raw)
        except LivePreparationError as error:
            raise LivePreparationError(
                "LIVE_BALANCE_INVALID",
                f"Не удалось прочитать баланс RUB: {raw!r}",
            ) from error

    @staticmethod
    def _balance_debit_matches(
        before: Decimal,
        after: Decimal,
        stake: float,
    ) -> bool:
        expected = Decimal(str(stake))
        actual = before - after
        return abs(actual - expected) <= BALANCE_TOLERANCE

    async def _visible_amount_input(self, page: Any) -> Any | None:
        inputs = page.locator(AMOUNT_SELECTOR)
        try:
            for index in range(await inputs.count()):
                candidate = inputs.nth(index)
                if await candidate.is_visible():
                    return candidate
        except Exception:
            return None
        return None

    async def _visible_confirm_button(self, page: Any) -> Any | None:
        buttons = page.locator(CONFIRM_SELECTOR)
        try:
            for index in range(await buttons.count()):
                candidate = buttons.nth(index)
                if not await candidate.is_visible():
                    continue
                text = " ".join((await candidate.inner_text()).split())
                if text.casefold() == CONFIRM_TEXT.casefold():
                    return candidate
        except Exception:
            return None
        return None

    @staticmethod
    def _confirm_click_definitely_not_dispatched(error: Exception) -> bool:
        """True only when Playwright timed out before it started the real click action."""
        message = str(error).casefold()
        return (
            "locator.click" in message
            and "waiting for element to be visible, enabled and stable" in message
            and "performing click action" not in message
        )

    async def _arm_confirm_button(self, confirm: Any, attempt_id: str) -> Any:
        """Attach the manual-click marker to the current Vue button instance."""
        handle = await confirm.element_handle()
        if handle is None:
            raise LivePreparationError(
                "LIVE_CONFIRM_BUTTON_NOT_FOUND",
                "Не удалось зафиксировать кнопку подтверждения.",
            )
        await handle.evaluate(
            """button => {
                button.dataset.autobetManualClick = '0';
                button.addEventListener('click', () => {
                    button.dataset.autobetManualClick = '1';
                }, { once: true });
            }"""
        )
        self._confirm_handles[attempt_id] = handle
        return handle

    async def _wait_for_ready_confirm_button(self, page: Any) -> tuple[Any | None, str]:
        """Allow the coupon to re-render and enable its button after stake entry."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_WAIT_SECONDS
        state = "NOT_FOUND"
        empty_since: float | None = None
        while True:
            if await self.blocked_event_exists(page):
                return None, "BLOCKED"
            empty_coupon = page.locator(EMPTY_COUPON_SELECTOR).first
            if await empty_coupon.count() and await empty_coupon.is_visible():
                if empty_since is None:
                    empty_since = loop.time()
                if loop.time() - empty_since >= 0.5:
                    return None, "EMPTY_COUPON"
            else:
                empty_since = None
            candidate = None if empty_since is not None else await self._visible_confirm_button(page)
            if candidate is not None:
                if not await candidate.is_disabled():
                    return candidate, "READY"
                state = "DISABLED"
            if loop.time() >= deadline:
                return None, "EMPTY_COUPON" if empty_since is not None else state
            await asyncio.sleep(0.10)

    async def _log_confirm_diagnostics(self, page: Any, attempt_id: str, state: str) -> None:
        try:
            buttons = page.locator(CONFIRM_FALLBACK_SELECTOR)
            count = await buttons.count()
            bets = page.locator(COUPON_BET_SELECTOR)
            bet_count = 0
            for index in range(await bets.count()):
                if await bets.nth(index).is_visible():
                    bet_count += 1
            details = []
            for index in range(min(count, 4)):
                button = buttons.nth(index)
                details.append(
                    {
                        "text": " ".join((await button.inner_text()).split()),
                        "visible": await button.is_visible(),
                        "disabled": await button.is_disabled(),
                    }
                )
            await self._log(
                "LIVE_CONFIRM_BUTTON_DIAGNOSTICS",
                f"attempt={attempt_id}; state={state}; coupon_bets={bet_count}; "
                f"coupon_buttons={count}; samples={details}",
            )
        except Exception as error:
            await self._log(
                "LIVE_CONFIRM_BUTTON_DIAGNOSTICS",
                f"attempt={attempt_id}; state={state}; inspection={type(error).__name__}",
            )

    async def _wait_for_coupon_surface(
        self,
        page: Any,
        *,
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Wait for the current coupon UI, without depending on a root wrapper.

        The bookmaker changed the outer coupon container.  The stable signals
        are the exact blocked-event text, the amount input, or the confirm
        button itself.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        empty_coupon_seen = False
        while loop.time() < deadline:
            if await self.blocked_event_exists(page):
                return "BLOCKED"
            if await self._visible_amount_input(page) is not None:
                return "AMOUNT_INPUT"
            if await self._visible_confirm_button(page) is not None:
                return "CONFIRM_BUTTON"
            empty_coupon = page.locator(EMPTY_COUPON_SELECTOR).first
            if await empty_coupon.count() and await empty_coupon.is_visible():
                empty_coupon_seen = True
            await asyncio.sleep(0.10)
        return "EMPTY_COUPON" if empty_coupon_seen else None

    async def _confirmed_blocked_container(self, page: Any) -> Any | None:
        """Return only the visible coupon lock with its exact confirmed text."""
        try:
            containers = page.locator(BLOCKED_COUPON_SELECTOR)
            expected = " ".join(BLOCKED_TEXT.casefold().split())
            for index in range(await containers.count()):
                container = containers.nth(index)
                if not await container.is_visible():
                    continue
                text = container.locator(BLOCKED_TEXT_SELECTOR).first
                if await text.count() == 0 or not await text.is_visible():
                    continue
                normalized = " ".join((await text.inner_text()).casefold().split())
                if normalized == expected:
                    return container
            return None
        except Exception:
            return None

    async def blocked_event_exists(self, page: Any) -> bool:
        return await self._confirmed_blocked_container(page) is not None

    async def _confirmed_success_modal(self, page: Any) -> Any | None:
        """Return only a visible modal that proves the bookmaker accepted the bet."""
        try:
            modals = page.locator(SUCCESS_MODAL_SELECTOR)
            expected = " ".join(SUCCESS_MODAL_TITLE.casefold().split())
            for index in range(await modals.count()):
                modal = modals.nth(index)
                if not await modal.is_visible():
                    continue

                # Preferred signal: the bookmaker's explicit success-title node.
                try:
                    title = modal.locator(SUCCESS_MODAL_TITLE_SELECTOR).first
                    if await title.count() and await title.is_visible():
                        actual = " ".join((await title.inner_text()).casefold().split())
                        if actual == expected:
                            return modal
                except Exception:
                    pass

                # Fallback for markup changes: the visible modal itself still
                # contains the exact accepted-bet text shown to the user.
                try:
                    modal_text = " ".join((await modal.inner_text()).casefold().split())
                    if expected in modal_text:
                        return modal
                except Exception:
                    pass
        except Exception:
            return None
        return None

    async def _wait_for_success_modal(
        self,
        page: Any,
        stop_event: asyncio.Event,
    ) -> Any | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SUCCESS_MODAL_WAIT_SECONDS
        while not stop_event.is_set() and loop.time() < deadline:
            modal = await self._confirmed_success_modal(page)
            if modal is not None:
                return modal
            await asyncio.sleep(0.10)
        return await self._confirmed_success_modal(page)

    async def _accept_success_modal(
        self,
        modal: Any,
        decision: LiveDecision,
    ) -> str:
        """Record the accepted coupon and always dismiss its blocking modal."""
        coupon_text = ""
        try:
            info = modal.locator(SUCCESS_MODAL_INFO_SELECTOR).first
            if await info.count() and await info.is_visible():
                coupon_text = " ".join((await info.inner_text()).split())
        except Exception:
            coupon_text = ""
        coupon_match = re.search(r"(?:Купон\\s*№\\s*)?(\\d{5,})", coupon_text, re.I)
        coupon_id = coupon_match.group(1) if coupon_match else "unknown"
        await self._log(
            "LIVE_SUCCESS_MODAL_DETECTED",
            f"attempt={decision.attempt_id}; coupon_id={coupon_id}",
        )

        async def modal_hidden() -> bool:
            try:
                return not await modal.is_visible()
            except Exception:
                return True

        async def wait_hidden(timeout_seconds: float = 0.45) -> bool:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_seconds
            while loop.time() < deadline:
                if await modal_hidden():
                    return True
                await asyncio.sleep(0.04)
            return await modal_hidden()

        # Preferred path: the green «Продолжить» button.
        continue_error: Exception | None = None
        try:
            continue_button = modal.locator(SUCCESS_MODAL_CONTINUE_SELECTOR).first
            if (
                await continue_button.count()
                and await continue_button.is_visible()
                and not await continue_button.is_disabled()
            ):
                await continue_button.evaluate("button => button.click()")
                if await wait_hidden():
                    await self._log(
                        "LIVE_SUCCESS_MODAL_CONTINUE_CLICKED",
                        f"attempt={decision.attempt_id}; coupon_id={coupon_id}",
                    )
                    return coupon_id
                await self._log(
                    "LIVE_SUCCESS_MODAL_CONTINUE_DID_NOT_CLOSE",
                    (
                        f"attempt={decision.attempt_id}; coupon_id={coupon_id}; "
                        "falling back to modal close control"
                    ),
                )
            else:
                continue_error = RuntimeError(
                    "Кнопка «Продолжить» отсутствует, скрыта или недоступна"
                )
        except Exception as error:
            continue_error = error

        # Fallback path: exact X button from the bookmaker modal DOM.
        try:
            close_button = modal.locator(SUCCESS_MODAL_CLOSE_SELECTOR).first
            if await close_button.count() == 0 or not await close_button.is_visible():
                raise RuntimeError("Кнопка закрытия success-modal не найдена")
            if await close_button.is_disabled():
                raise RuntimeError("Кнопка закрытия success-modal недоступна")

            await close_button.evaluate("button => button.click()")
            if await wait_hidden():
                await self._log(
                    "LIVE_SUCCESS_MODAL_CLOSE_CLICKED",
                    (
                        f"attempt={decision.attempt_id}; coupon_id={coupon_id}; "
                        "fallback=modal-control"
                    ),
                )
            else:
                await self._log(
                    "LIVE_SUCCESS_MODAL_STILL_VISIBLE",
                    (
                        f"attempt={decision.attempt_id}; coupon_id={coupon_id}; "
                        "continue/close controls were triggered but modal stayed visible"
                    ),
                )
        except Exception as close_error:
            # Acceptance itself is already proven by the exact success title.
            # Modal cleanup failure must never create a second real-money click.
            await self._log(
                "LIVE_SUCCESS_MODAL_CLOSE_FAILED",
                (
                    f"attempt={decision.attempt_id}; coupon_id={coupon_id}; "
                    f"continue_error={type(continue_error).__name__ if continue_error else 'none'}; "
                    f"close_error={type(close_error).__name__}: {close_error}"
                ),
            )
        return coupon_id

    async def _dismiss_success_modal_after_balance_debit(
        self,
        page: Any,
        decision: LiveDecision,
    ) -> None:
        """Close a delayed success modal before any next LIVE interaction."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SUCCESS_MODAL_BALANCE_RACE_SECONDS
        while True:
            modal = await self._confirmed_success_modal(page)
            if modal is not None:
                await self._log(
                    "LIVE_SUCCESS_MODAL_CLEANUP_AFTER_BALANCE",
                    f"attempt={decision.attempt_id}",
                )
                await self._accept_success_modal(modal, decision)

                # Reacquire from the page, not from the old locator. Vue can
                # replace the modal node while handling Continue/X.
                if await self._confirmed_success_modal(page) is None:
                    return
                await self._log(
                    "LIVE_SUCCESS_MODAL_CLEANUP_RETRY",
                    f"attempt={decision.attempt_id}; modal still visible after first cleanup",
                )

            if loop.time() >= deadline:
                remaining = await self._confirmed_success_modal(page)
                if remaining is not None:
                    await self._log(
                        "LIVE_SUCCESS_MODAL_CLEANUP_EXHAUSTED",
                        (
                            f"attempt={decision.attempt_id}; "
                            "accepted modal is still visible after cleanup window"
                        ),
                    )
                return
            await asyncio.sleep(0.05)

    async def _publish_accepted_placement(
        self,
        decision: LiveDecision,
        publish: Publisher | None,
        message: str,
    ) -> None:
        await self._publish(
            decision.attempt_id,
            LiveStatus.BET_PLACED,
            message,
            publish,
        )
        await self._publish(
            decision.attempt_id,
            LiveStatus.ACTIVE,
            "LIVE-ставка активна",
            publish,
        )

    async def _finish_success_modal_placement(
        self,
        modal: Any,
        decision: LiveDecision,
        publish: Publisher | None,
    ) -> PlacementObservation:
        coupon_id = await self._accept_success_modal(modal, decision)
        await self._publish_accepted_placement(
            decision,
            publish,
            f"Букмекер подтвердил принятие ставки; coupon_id={coupon_id}",
        )
        return PlacementObservation(
            True,
            f"success modal confirmed; coupon_id={coupon_id}",
        )

    async def _verify_coupon_selection(self, page: Any, decision: LiveDecision) -> None:
        """Check a Next Goal coupon before submitting real money."""
        if decision.side.value not in {"TEAM_1", "TEAM_2"}:
            return  # The shared executor also handles first-half draw.
        bets = page.locator(COUPON_BET_SELECTOR)
        visible = [
            bets.nth(index)
            for index in range(await bets.count())
            if await bets.nth(index).is_visible()
        ]
        if len(visible) != 1:
            raise LivePreparationError(
                "LIVE_COUPON_SELECTION_UNKNOWN",
                f"Ожидалась одна выбранная ставка в купоне, найдено: {len(visible)}.",
            )
        bet = visible[0]
        side_selector = (
            COUPON_FIRST_TEAM_SELECTOR
            if decision.side.value == "TEAM_1"
            else COUPON_SECOND_TEAM_SELECTOR
        )
        team = bet.locator(side_selector).first
        market = bet.locator(COUPON_MARKET_SELECTOR).first
        if await team.count() == 0 or await market.count() == 0:
            raise LivePreparationError(
                "LIVE_COUPON_SELECTION_UNKNOWN",
                "В купоне нет названия выбранной команды или рынка.",
            )
        actual_team = " ".join((await team.inner_text()).casefold().split())
        expected_team = " ".join(decision.team.casefold().split())
        market_text = " ".join((await market.inner_text()).split())
        side_number = 1 if decision.side.value == "TEAM_1" else 2
        match = re.search(
            r"Следующий\s+гол\s*:\s*Команда\s*([12])\s*[-–]\s*(\d+)-[а-яё]+\s+гол",
            market_text,
            re.I,
        )
        if (
            actual_team != expected_team
            or match is None
            or int(match.group(1)) != side_number
            or int(match.group(2)) != decision.goal_number
        ):
            raise LivePreparationError(
                "LIVE_COUPON_SELECTION_MISMATCH",
                f"Купон: {actual_team!r}, {market_text!r}; ожидались "
                f"{decision.team!r}, команда {side_number}, гол №{decision.goal_number}.",
            )

    async def _wait_for_coupon_selection(
        self,
        page: Any,
        decision: LiveDecision,
        *,
        timeout_seconds: float = COUPON_SELECTION_WAIT_SECONDS,
    ) -> str:
        """Wait until the selected outcome finishes rendering inside coupon.

        The amount input can become visible before Vue mounts the coupon bet
        card.  Treating that short intermediate state as a malformed coupon
        stopped LIVE even though no stake had been filled or submitted.
        """
        if decision.side.value not in {"TEAM_1", "TEAM_2"}:
            return "VERIFIED"

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + timeout_seconds
        wait_logged = False
        last_count = 0
        last_details = "карточка исхода отсутствует"

        while loop.time() < deadline:
            if await self.blocked_event_exists(page):
                return "BLOCKED"

            try:
                bets = page.locator(COUPON_BET_SELECTOR)
                visible = [
                    bets.nth(index)
                    for index in range(await bets.count())
                    if await bets.nth(index).is_visible()
                ]
                last_count = len(visible)

                # Multiple selections are not a render delay. Submitting in
                # this state could place an unintended accumulator.
                if last_count > 1:
                    raise LivePreparationError(
                        "LIVE_COUPON_SELECTION_UNKNOWN",
                        f"Ожидалась одна выбранная ставка в купоне, найдено: {last_count}.",
                    )

                if last_count == 1:
                    try:
                        await self._verify_coupon_selection(page, decision)
                    except LivePreparationError as error:
                        if error.status != "LIVE_COUPON_SELECTION_UNKNOWN":
                            raise
                        # The card exists, but its team/market children can be
                        # mounted one render tick later.
                        last_details = str(error)
                    else:
                        waited_ms = int((loop.time() - started_at) * 1000)
                        if wait_logged:
                            await self._log(
                                "LIVE_COUPON_SELECTION_RENDERED",
                                f"attempt={decision.attempt_id}; waited_ms={waited_ms}",
                            )
                        return "VERIFIED"
                else:
                    last_details = "карточка исхода отсутствует"
            except LivePreparationError:
                raise
            except Exception as error:
                # Coupon children can be replaced while Vue is rendering.
                # Retry only before any amount or confirmation interaction.
                last_details = f"{type(error).__name__}: {error}"

            if not wait_logged:
                wait_logged = True
                await self._log(
                    "LIVE_COUPON_SELECTION_RENDER_WAIT",
                    (
                        f"attempt={decision.attempt_id}; amount/confirm surface is visible, "
                        "waiting for selected outcome card"
                    ),
                )
            await asyncio.sleep(0.10)

        raise LivePreparationError(
            "LIVE_COUPON_SELECTION_NOT_RENDERED",
            (
                "Coupon открылся, но выбранный исход не отрисовался за "
                f"{timeout_seconds:g} сек.; visible_bets={last_count}; {last_details}. "
                "Сумма не вводилась, ставка не отправлялась."
            ),
        )

    async def clear_unaccepted_coupon(self, page: Any, attempt_id: str) -> bool:
        """Clear a proven-unaccepted coupon without relying on lock strategy.

        The method reacquires DOM nodes on every pass because Vue can replace
        coupon elements during market updates. Disabled controls are never
        force-clicked.
        """
        async with self._lock:
            for retry in range(1, 6):
                empty_coupon = page.locator(EMPTY_COUPON_SELECTOR).first
                if await empty_coupon.count() and await empty_coupon.is_visible():
                    await self._log(
                        "LIVE_UNACCEPTED_COUPON_ALREADY_CLEAR",
                        f"attempt={attempt_id}; retry={retry}",
                    )
                    return True

                visible_bet = False
                removed = False
                bets = page.locator(COUPON_BET_SELECTOR)
                try:
                    bet_count = await bets.count()
                except Exception:
                    bet_count = 0

                for index in range(bet_count):
                    bet = bets.nth(index)
                    try:
                        if not await bet.is_visible():
                            continue
                        visible_bet = True
                        remove = bet.locator(COUPON_REMOVE_SELECTOR).first
                        if (
                            await remove.count()
                            and await remove.is_visible()
                            and not await remove.is_disabled()
                        ):
                            await self._log(
                                "LIVE_UNACCEPTED_COUPON_CLEAR_ATTEMPT",
                                (
                                    f"attempt={attempt_id}; retry={retry}; "
                                    "source=regular_coupon"
                                ),
                            )
                            await remove.click(timeout=CONFIRM_CLICK_TIMEOUT_MS)
                            removed = True
                            break
                    except Exception as error:
                        await self._log(
                            "LIVE_UNACCEPTED_COUPON_CLEAR_TRANSIENT",
                            (
                                f"attempt={attempt_id}; retry={retry}; "
                                f"{type(error).__name__}: {error}"
                            ),
                        )

                if not removed:
                    blocked = await self._confirmed_blocked_container(page)
                    if blocked is not None:
                        visible_bet = True
                        for selector in (
                            BLOCKED_REMOVE_SELECTOR,
                            BLOCKED_REMOVE_FALLBACK_SELECTOR,
                        ):
                            try:
                                remove = blocked.locator(selector).first
                                if (
                                    await remove.count()
                                    and await remove.is_visible()
                                    and not await remove.is_disabled()
                                ):
                                    await self._log(
                                        "LIVE_UNACCEPTED_COUPON_CLEAR_ATTEMPT",
                                        (
                                            f"attempt={attempt_id}; retry={retry}; "
                                            "source=blocked_coupon"
                                        ),
                                    )
                                    await remove.click(
                                        timeout=CONFIRM_CLICK_TIMEOUT_MS
                                    )
                                    removed = True
                                    break
                            except Exception as error:
                                await self._log(
                                    "LIVE_UNACCEPTED_COUPON_CLEAR_TRANSIENT",
                                    (
                                        f"attempt={attempt_id}; retry={retry}; "
                                        f"{type(error).__name__}: {error}"
                                    ),
                                )

                await asyncio.sleep(0.10)

                # Reacquire everything after the click/re-render.
                empty_coupon = page.locator(EMPTY_COUPON_SELECTOR).first
                if await empty_coupon.count() and await empty_coupon.is_visible():
                    await self._log(
                        "LIVE_UNACCEPTED_COUPON_CLEARED",
                        f"attempt={attempt_id}; retry={retry}",
                    )
                    return True

                remaining_visible = False
                bets = page.locator(COUPON_BET_SELECTOR)
                try:
                    for index in range(await bets.count()):
                        if await bets.nth(index).is_visible():
                            remaining_visible = True
                            break
                except Exception:
                    remaining_visible = True

                blocked_remaining = await self.blocked_event_exists(page)
                if not remaining_visible and not blocked_remaining:
                    amount_input = await self._visible_amount_input(page)
                    confirm = await self._visible_confirm_button(page)
                    if amount_input is None and confirm is None:
                        await self._log(
                            "LIVE_UNACCEPTED_COUPON_CLEARED",
                            f"attempt={attempt_id}; retry={retry}; surface=gone",
                        )
                        return True

                if not visible_bet and not blocked_remaining:
                    # No coupon selection is present; this is already a clean
                    # state even when the empty-coupon placeholder is absent.
                    await self._log(
                        "LIVE_UNACCEPTED_COUPON_ALREADY_CLEAR",
                        f"attempt={attempt_id}; retry={retry}; no_selection=true",
                    )
                    return True

            await self._log(
                "LIVE_UNACCEPTED_COUPON_CLEAR_FAILED",
                f"attempt={attempt_id}; retries=5",
            )
            return False

    async def remove_blocked_coupon(self, page: Any, attempt_id: str) -> bool:
        """Remove one rejected coupon exactly once for a placement attempt."""
        async with self._lock:
            if attempt_id in self._recovered_blocked_attempts:
                return False

            blocked = await self._confirmed_blocked_container(page)
            if blocked is None:
                raise LivePreparationError(
                    "LIVE_BLOCKED_COUPON_NOT_CONFIRMED",
                    "Заблокированный coupon с точным текстом не подтверждён.",
                )

            remove = blocked.locator(BLOCKED_REMOVE_SELECTOR).first
            if await remove.count() == 0 or not await remove.is_visible():
                remove = blocked.locator(BLOCKED_REMOVE_FALLBACK_SELECTOR).first
            if await remove.count() == 0 or not await remove.is_visible():
                raise LivePreparationError(
                    "LIVE_BLOCKED_REMOVE_NOT_FOUND",
                    "Кнопка удаления заблокированного события не найдена.",
                )

            await self._log("LIVE_BLOCKED_COUPON_REMOVING", f"attempt={attempt_id}")
            try:
                await remove.click(timeout=5_000)
                await blocked.wait_for(state="hidden", timeout=5_000)
                if await self.blocked_event_exists(page):
                    raise LivePreparationError(
                        "LIVE_BLOCKED_COUPON_NOT_REMOVED",
                        "Заблокированное событие осталось в coupon после удаления.",
                    )
            except LivePreparationError:
                raise
            except Exception as error:
                raise LivePreparationError(
                    "LIVE_BLOCKED_COUPON_NOT_REMOVED",
                    f"Не удалось удалить заблокированное событие: {error}",
                ) from error

            self._recovered_blocked_attempts.add(attempt_id)
            await self._log("LIVE_BLOCKED_COUPON_REMOVED", f"attempt={attempt_id}")
            return True

    async def _clear_accepted_coupon(self, page: Any, decision: LiveDecision) -> None:
        """Clear a lingering accepted selection without blocking the LIVE loop."""
        if decision.side.value not in {"TEAM_1", "TEAM_2"}:
            return
        try:
            bets = page.locator(COUPON_BET_SELECTOR)
            if await bets.count() != 1 or not await bets.first.is_visible():
                return
            await self._verify_coupon_selection(page, decision)
            remove = bets.first.locator(COUPON_REMOVE_SELECTOR).first
            if await remove.count() == 0 or not await remove.is_visible():
                return

            # The debit already proves acceptance. Avoid Playwright's 5-second
            # actionability wait on a Vue node that is frequently detached.
            try:
                await remove.evaluate("button => button.click()")
            except Exception:
                # A detached button after acceptance generally means the coupon
                # was already cleared by the bookmaker rerender.
                pass

            loop = asyncio.get_running_loop()
            deadline = loop.time() + ACCEPTED_COUPON_CLEANUP_SECONDS
            while loop.time() < deadline:
                current = page.locator(COUPON_BET_SELECTOR)
                if await current.count() == 0 or not await current.first.is_visible():
                    await self._log(
                        "LIVE_ACCEPTED_COUPON_CLEARED",
                        f"attempt={decision.attempt_id}",
                    )
                    return
                await asyncio.sleep(0.04)

            await self._log(
                "LIVE_ACCEPTED_COUPON_CLEANUP_SKIPPED",
                (
                    f"attempt={decision.attempt_id}; "
                    "coupon still visible after fast cleanup window"
                ),
            )
        except Exception as error:
            # The debit has already confirmed the bet; cleanup cannot undo it.
            await self._log(
                "LIVE_ACCEPTED_COUPON_CLEANUP_FAILED",
                f"attempt={decision.attempt_id}; {type(error).__name__}: {error}",
            )

    @staticmethod
    def _submission_deadline_reached(decision: LiveDecision) -> bool:
        deadline = decision.submission_deadline_monotonic
        return deadline is not None and time.monotonic() >= deadline

    def _raise_if_submission_deadline_reached(
        self,
        decision: LiveDecision,
        *,
        phase: str,
    ) -> None:
        if self._submission_deadline_reached(decision):
            raise LivePreparationError(
                "LIVE_RUN_TIME_LIMIT_REACHED",
                (
                    "Лимит времени работы истёк до отправки новой ставки; "
                    f"phase={phase}; step={decision.strategy_step}"
                ),
            )

    async def prepare(
        self,
        page: Any,
        decision: LiveDecision,
        publish: Publisher | None = None,
    ) -> None:
        async with self._lock:
            if self.state(decision.attempt_id) != LiveStatus.IDLE:
                raise LivePreparationError(
                    "DUPLICATE_LIVE_PREPARATION",
                    f"Попытка {decision.attempt_id} уже подготавливалась.",
                )
            if decision.coefficient_locator is None:
                raise LivePreparationError(
                    "LIVE_MARKET_ELEMENT_MISSING", "DOM-элемент выбранного коэффициента отсутствует."
                )
            self._raise_if_submission_deadline_reached(
                decision,
                phase="before_market_click",
            )

            try:
                await decision.coefficient_locator.click()
                self._market_selected.add(decision.attempt_id)
                click_details = getattr(decision.coefficient_locator, "last_click", None)
                if click_details is not None:
                    await self._log(
                        "LIVE_CANVAS_CLICK_TARGET",
                        f"attempt={decision.attempt_id}; {click_details}",
                    )
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_MARKET_SELECTED,
                    f"goal={decision.goal_number} team={decision.side.value} odds={decision.coefficient}",
                    publish,
                )

                coupon_signal = await self._wait_for_coupon_surface(page)
                if coupon_signal == "EMPTY_COUPON":
                    raise LivePreparationError(
                        "LIVE_COUPON_EMPTY_AFTER_CLICK",
                        "После Canvas-клика купон остался пустым: исход не добавлен. "
                        "Ставка не отправлена; повторный клик без проверки не выполняется.",
                    )
                if coupon_signal is None:
                    raise LivePreparationError(
                        "LIVE_COUPON_NOT_READY",
                        (
                            "После клика по коэффициенту не появились признаки coupon: "
                            "ни «Заблокированное событие», ни поле суммы, "
                            "ни кнопка «Сделать ставку»."
                        ),
                    )
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_COUPON_OPENED,
                    f"Coupon открыт; signal={coupon_signal}",
                    publish,
                )

                # LIVE rule: exact bookmaker lock is handled before amount input.
                if coupon_signal == "BLOCKED" or await self.blocked_event_exists(page):
                    await self._log(
                        "LIVE_BLOCKED_EVENT_DETECTED",
                        (
                            f"attempt={decision.attempt_id}; phase=after_market_click; "
                            "coupon contains exact text 'Заблокированное событие'"
                        ),
                    )
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Coupon заблокирован; ставка не отправляется",
                        publish,
                    )
                    return

                selection_signal = await self._wait_for_coupon_selection(page, decision)
                if selection_signal == "BLOCKED":
                    await self._log(
                        "LIVE_BLOCKED_EVENT_DETECTED",
                        f"attempt={decision.attempt_id}; phase=waiting_coupon_selection",
                    )
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Coupon заблокирован; ставка не отправляется",
                        publish,
                    )
                    return
                await self._log(
                    "LIVE_COUPON_SELECTION_VERIFIED",
                    f"attempt={decision.attempt_id}; team={decision.team}; goal={decision.goal_number}",
                )

                # Keep the old account-control check only when that old DOM
                # control still exists. The current site is validated by the
                # real RUB balance in the header.
                account = page.locator(ACCOUNT_SELECTOR).first
                if await account.count() > 0 and await account.is_visible():
                    pressed = await account.get_attribute("aria-pressed")
                    if pressed is not None and pressed.lower() == "false":
                        raise LivePreparationError(
                            "LIVE_RUB_ACCOUNT_NOT_SELECTED",
                            "Счёт «Основной (RUB)» не выбран.",
                        )

                amount_input = await self._visible_amount_input(page)
                if amount_input is None:
                    raise LivePreparationError(
                        "LIVE_AMOUNT_INPUT_NOT_FOUND",
                        "Поле суммы в coupon не найдено.",
                    )
                expected_text = _amount_text(decision.amount)
                await amount_input.fill("")
                await amount_input.fill(expected_text)
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_AMOUNT_FILLED,
                    f"step={decision.strategy_step} amount={expected_text}",
                    publish,
                )
                actual = _parse_amount(await amount_input.input_value())
                if actual != Decimal(str(decision.amount)):
                    raise LivePreparationError(
                        "LIVE_AMOUNT_VERIFICATION_FAILED",
                        f"Ожидалась сумма {expected_text}, поле содержит {actual}.",
                    )
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_AMOUNT_VERIFIED,
                    f"Сумма подтверждена: {expected_text}",
                    publish,
                )

                balance_before = await self._read_balance(page)
                self._balance_before[decision.attempt_id] = balance_before
                await self._log(
                    "LIVE_BALANCE_BEFORE",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"balance={balance_before}; stake={expected_text}"
                    ),
                )

                confirm, confirm_state = await self._wait_for_ready_confirm_button(page)
                if confirm_state == "BLOCKED":
                    await self._log(
                        "LIVE_BLOCKED_EVENT_DETECTED",
                        f"attempt={decision.attempt_id}; phase=before_confirm_click",
                    )
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Coupon заблокирован; ставка не отправляется",
                        publish,
                    )
                    return
                if confirm is None:
                    await self._log_confirm_diagnostics(page, decision.attempt_id, confirm_state)
                    if confirm_state == "EMPTY_COUPON":
                        raise LivePreparationError(
                            "LIVE_COUPON_LOST_BEFORE_CONFIRM",
                            "Исход исчез из купона до подтверждения. Ставка не отправлена.",
                        )
                    if confirm_state == "DISABLED":
                        raise LivePreparationError(
                            "LIVE_CONFIRM_BUTTON_NOT_READY",
                            "Кнопка «Сделать ставку» осталась недоступна.",
                        )
                    raise LivePreparationError(
                        "LIVE_CONFIRM_BUTTON_NOT_FOUND",
                        "Кнопка «Сделать ставку» не найдена в купоне.",
                    )
                # Recheck the selection and stake after waiting for site updates.
                await self._verify_coupon_selection(page, decision)
                if _parse_amount(await amount_input.input_value()) != Decimal(str(decision.amount)):
                    raise LivePreparationError(
                        "LIVE_AMOUNT_VERIFICATION_FAILED", "Сумма ставки изменилась перед подтверждением."
                    )
                # Сохраняем handle и listener: это оставляет прежний ручной режим рабочим,
                # когда автоподтверждение тестового аккаунта выключено.
                await self._arm_confirm_button(confirm, decision.attempt_id)

                if not TEST_AUTO_CONFIRM:
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.READY_FOR_MANUAL_CONFIRMATION,
                        "Coupon проверен. Нажмите «Сделать ставку» один раз вручную.",
                        publish,
                    )
                    return

                # ТЕСТОВЫЙ АККАУНТ: после полной проверки coupon автоматически
                # нажимаем ту же кнопку, которую раньше должен был нажать пользователь.
                await self._log(
                    "TEST_AUTO_CONFIRM",
                    (
                        f"attempt={decision.attempt_id} "
                        f"step={decision.strategy_step} amount={expected_text}"
                    ),
                )

                current_confirm = confirm
                click_message = ""
                for click_try in range(1, CONFIRM_PRE_DISPATCH_RETRIES + 1):
                    self._raise_if_submission_deadline_reached(
                        decision,
                        phase="before_confirm_click",
                    )
                    await self._log(
                        "LIVE_COUPON_CLICK_ATTEMPT",
                        (
                            f"attempt={decision.attempt_id}; "
                            f"step={decision.strategy_step}; retry={click_try}"
                        ),
                    )
                    try:
                        await current_confirm.click(timeout=CONFIRM_CLICK_TIMEOUT_MS)
                    except Exception as error:
                        definitely_not_dispatched = (
                            self._confirm_click_definitely_not_dispatched(error)
                        )
                        if (
                            definitely_not_dispatched
                            and click_try < CONFIRM_PRE_DISPATCH_RETRIES
                        ):
                            await self._log(
                                "LIVE_COUPON_BUTTON_REACQUIRE",
                                (
                                    f"attempt={decision.attempt_id}; retry={click_try}; "
                                    "Playwright did not reach performing click action; "
                                    "reacquiring fresh Vue button"
                                ),
                            )
                            await asyncio.sleep(CONFIRM_PRE_DISPATCH_RETRY_DELAY_SECONDS)

                            current_confirm, confirm_state = (
                                await self._wait_for_ready_confirm_button(page)
                            )
                            if confirm_state == "BLOCKED":
                                await self._publish(
                                    decision.attempt_id,
                                    LiveStatus.AWAITING_PLACEMENT_RESULT,
                                    "Coupon заблокирован до подтверждения; ставка не отправлена",
                                    publish,
                                )
                                return
                            if current_confirm is None:
                                raise LivePreparationError(
                                    "LIVE_CONFIRM_BUTTON_NOT_READY",
                                    (
                                        "Кнопка «Сделать ставку» исчезла при "
                                        "повторном получении после Vue rerender."
                                    ),
                                )

                            # The coupon may have been replaced together with the
                            # button. Revalidate every safety boundary before retry.
                            await self._verify_coupon_selection(page, decision)
                            fresh_amount_input = await self._visible_amount_input(page)
                            if fresh_amount_input is None:
                                raise LivePreparationError(
                                    "LIVE_AMOUNT_INPUT_NOT_FOUND",
                                    "Поле суммы исчезло перед повторным подтверждением.",
                                )
                            if _parse_amount(
                                await fresh_amount_input.input_value()
                            ) != Decimal(str(decision.amount)):
                                raise LivePreparationError(
                                    "LIVE_AMOUNT_VERIFICATION_FAILED",
                                    "Сумма ставки изменилась перед повторным подтверждением.",
                                )
                            await self._arm_confirm_button(
                                current_confirm,
                                decision.attempt_id,
                            )
                            continue

                        if definitely_not_dispatched:
                            await self._log(
                                "LIVE_COUPON_BUTTON_NOT_CLICKED",
                                (
                                    f"attempt={decision.attempt_id}; "
                                    f"retries={click_try}; "
                                    "button stayed unstable before click dispatch"
                                ),
                            )
                            raise LivePreparationError(
                                "LIVE_CONFIRM_BUTTON_UNSTABLE_NOT_CLICKED",
                                (
                                    "Кнопка «Сделать ставку» несколько раз "
                                    "перерисовалась до фактического клика. "
                                    "Ставка не отправлена."
                                ),
                            ) from error

                        # Once Playwright reached the real click action, its result
                        # is ambiguous. Never issue a second click in this attempt.
                        self._submission_attempted.add(decision.attempt_id)
                        await self._log(
                            "LIVE_COUPON_BUTTON_UNSTABLE",
                            (
                                f"attempt={decision.attempt_id}; "
                                f"{type(error).__name__}: {error}"
                            ),
                        )
                        click_message = (
                            "Клик мог быть отправлен; проверяем ACCEPTED без второго клика"
                        )
                        break
                    else:
                        self._submission_attempted.add(decision.attempt_id)
                        await self._log(
                            "LIVE_COUPON_CLICKED",
                            (
                                f"attempt={decision.attempt_id}; "
                                f"retry={click_try}"
                            ),
                        )
                        click_message = (
                            "Тестовый режим: кнопка «Сделать ставку» нажата автоматически; "
                            "ждём ответ сайта"
                        )
                        break

                await self._publish(
                    decision.attempt_id,
                    LiveStatus.AWAITING_PLACEMENT_RESULT,
                    click_message,
                    publish,
                )
            except LivePreparationError:
                self._states[decision.attempt_id] = LiveStatus.ERROR
                raise
            except Exception as error:
                self._states[decision.attempt_id] = LiveStatus.ERROR
                raise LivePreparationError(
                    "LIVE_PREPARATION_FAILED", f"Не удалось подготовить LIVE coupon: {error}"
                ) from error

    async def wait_for_manual_confirmation(
        self,
        page: Any,
        decision: LiveDecision,
        stop_event: asyncio.Event,
        publish: Publisher | None = None,
    ) -> PlacementObservation | None:
        current_state = self.state(decision.attempt_id)
        allowed_states = {
            LiveStatus.READY_FOR_MANUAL_CONFIRMATION,
            LiveStatus.AWAITING_PLACEMENT_RESULT,
        }
        if current_state not in allowed_states:
            raise LivePreparationError(
                "LIVE_INVALID_STATE",
                f"Coupon находится в неподходящем состоянии: {current_state.value}",
            )

        click_seen = await self.manual_click_seen(decision.attempt_id)
        reconcile_deadline: float | None = None
        loop = asyncio.get_running_loop()
        balance_read_failures = 0

        while not stop_event.is_set():
            if not click_seen:
                click_seen = await self.manual_click_seen(decision.attempt_id)
                if click_seen:
                    reconcile_deadline = loop.time() + SUBMISSION_RECONCILE_SECONDS
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Клик подтверждения обнаружен; проверяем принятие ставки",
                        publish,
                    )

            if not click_seen:
                if await self.blocked_event_exists(page):
                    return PlacementObservation(
                        False,
                        BLOCKED_EVENT_SIGNAL,
                        retryable=True,
                    )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.10)
                    continue
                except TimeoutError:
                    continue

            if reconcile_deadline is None:
                reconcile_deadline = loop.time() + SUBMISSION_RECONCILE_SECONDS

            modal = await self._confirmed_success_modal(page)
            if modal is not None:
                return await self._finish_success_modal_placement(
                    modal,
                    decision,
                    publish,
                )

            if await self.blocked_event_exists(page):
                await self._log(
                    "LIVE_SUBMISSION_NOT_ACCEPTED",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"signal={BLOCKED_EVENT_SIGNAL}"
                    ),
                )
                return PlacementObservation(
                    False,
                    BLOCKED_EVENT_SIGNAL,
                    retryable=True,
                )

            balance_before = self._balance_before.get(decision.attempt_id)
            if balance_before is None:
                raise LivePreparationError(
                    "LIVE_BALANCE_BEFORE_MISSING",
                    "Нет сохранённого баланса перед отправкой ставки.",
                )

            try:
                balance_after = await self._read_balance(page)
                balance_read_failures = 0
            except LivePreparationError as error:
                balance_read_failures += 1
                await self._log(
                    "LIVE_BALANCE_RECONCILE_READ_FAILED",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"failure={balance_read_failures}; {error}"
                    ),
                )
                if loop.time() >= reconcile_deadline:
                    return None
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.25)
                except TimeoutError:
                    pass
                continue

            debit = balance_before - balance_after
            await self._log(
                "LIVE_BALANCE_AFTER",
                (
                    f"attempt={decision.attempt_id}; before={balance_before}; "
                    f"after={balance_after}; debit={debit}; stake={decision.amount}"
                ),
            )

            if self._balance_debit_matches(
                balance_before,
                balance_after,
                decision.amount,
            ):
                await self._log(
                    "LIVE_BALANCE_DEBIT_CONFIRMED",
                    (
                        f"attempt={decision.attempt_id}; debit={debit}; "
                        f"stake={decision.amount}"
                    ),
                )
                await self._dismiss_success_modal_after_balance_debit(
                    page,
                    decision,
                )
                await self._clear_accepted_coupon(page, decision)
                await self._publish_accepted_placement(
                    decision,
                    publish,
                    "Баланс уменьшился на сумму шага; ставка размещена",
                )
                return PlacementObservation(
                    True,
                    "balance debit confirmed",
                )

            if loop.time() >= reconcile_deadline:
                # One final modal check closes the race where the balance/header
                # update lags behind the bookmaker success response.
                modal = await self._confirmed_success_modal(page)
                if modal is not None:
                    return await self._finish_success_modal_placement(
                        modal,
                        decision,
                        publish,
                    )
                await self._log(
                    "LIVE_SUBMISSION_NOT_ACCEPTED",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"before={balance_before}; after={balance_after}; "
                        f"stake={decision.amount}; no_success_modal=true"
                    ),
                )
                return PlacementObservation(
                    False,
                    "NOT_ACCEPTED_NO_CONFIRMATION",
                    retryable=True,
                )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.25)
            except TimeoutError:
                pass

        return None
    async def invalidate(
        self,
        attempt_id: str,
        reason: str,
        publish: Publisher | None = None,
    ) -> None:
        await self._publish(attempt_id, LiveStatus.STALE_COUPON, reason, publish)
        self._confirm_handles.pop(attempt_id, None)

    def reset(self) -> None:
        self._states.clear()
        self._confirm_handles.clear()
        self._market_selected.clear()
        self._submission_attempted.clear()
        self._recovered_blocked_attempts.clear()
        self._balance_before.clear()
