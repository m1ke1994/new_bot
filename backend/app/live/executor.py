import asyncio
import re
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
    'input.ui-number-input__field[type="text"][inputmode="decimal"], '
    ".coupon-app input.ui-number-input__field, "
    '.quick-coupon-main input.ui-number-input__field[placeholder="Введите сумму ставки"]'
)
CONFIRM_BUTTON_CLASS_SELECTOR = (
    "button.ui-button.ui-button--size-m.ui-button--theme-accent"
    ".ui-button--block.ui-button--uppercase.ui-button--rounded"
)
CONFIRM_SELECTOR = (
    f"{CONFIRM_BUTTON_CLASS_SELECTOR}, "
    f".coupon-buttons {CONFIRM_BUTTON_CLASS_SELECTOR}, "
    f".coupon-app {CONFIRM_BUTTON_CLASS_SELECTOR}, "
    ".quick-coupon-main button.quick-coupon-put-bet-button"
)
CONFIRM_TEXT = "Сделать ставку"
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
        handle = self._confirm_handles.get(attempt_id)
        if handle is None:
            return False
        try:
            return await handle.get_attribute("data-autobet-manual-click") == "1"
        except Exception:
            return self.state(attempt_id) == LiveStatus.AWAITING_PLACEMENT_RESULT

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
                if CONFIRM_TEXT in text:
                    return candidate
        except Exception:
            return None
        return None

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
        """Clear a lingering selection after a confirmed debit, if it is still ours."""
        if decision.side.value not in {"TEAM_1", "TEAM_2"}:
            return
        try:
            bets = page.locator(COUPON_BET_SELECTOR)
            if await bets.count() != 1 or not await bets.first.is_visible():
                return
            await self._verify_coupon_selection(page, decision)
            remove = bets.first.locator(COUPON_REMOVE_SELECTOR).first
            if await remove.count() and await remove.is_visible():
                await remove.click(timeout=5_000)
                await self._log("LIVE_ACCEPTED_COUPON_CLEARED", f"attempt={decision.attempt_id}")
        except Exception as error:
            # The debit has already confirmed the bet; cleanup cannot undo it.
            await self._log(
                "LIVE_ACCEPTED_COUPON_CLEANUP_FAILED",
                f"attempt={decision.attempt_id}; {type(error).__name__}: {error}",
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

                await self._verify_coupon_selection(page, decision)

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

                confirm = await self._visible_confirm_button(page)
                if confirm is None:
                    raise LivePreparationError(
                        "LIVE_CONFIRM_BUTTON_NOT_FOUND",
                        "Кнопка «Сделать ставку» не найдена.",
                    )
                if await confirm.is_disabled() or CONFIRM_TEXT not in (await confirm.inner_text()):
                    raise LivePreparationError(
                        "LIVE_CONFIRM_BUTTON_NOT_READY", "Кнопка «Сделать ставку» недоступна."
                    )
                handle = await confirm.element_handle()
                if handle is None:
                    raise LivePreparationError(
                        "LIVE_CONFIRM_BUTTON_NOT_FOUND", "Не удалось зафиксировать кнопку подтверждения."
                    )

                # Сохраняем handle и listener: это оставляет прежний ручной режим рабочим,
                # когда автоподтверждение тестового аккаунта выключено.
                await handle.evaluate(
                    """button => {
                        button.dataset.autobetManualClick = '0';
                        button.addEventListener('click', () => {
                            button.dataset.autobetManualClick = '1';
                        }, { once: true });
                    }"""
                )
                self._confirm_handles[decision.attempt_id] = handle

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
                await confirm.click(timeout=5_000)
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.AWAITING_PLACEMENT_RESULT,
                    "Тестовый режим: кнопка «Сделать ставку» нажата автоматически; ждём ответ сайта",
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

        click_seen = current_state == LiveStatus.AWAITING_PLACEMENT_RESULT
        reloaded_after_missing_debit = False

        while not stop_event.is_set():
            if await self.blocked_event_exists(page):
                await self._log(
                    "LIVE_BLOCKED_EVENT_DETECTED",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"status={self.state(decision.attempt_id).value}"
                    ),
                )
                return PlacementObservation(
                    False,
                    BLOCKED_EVENT_SIGNAL,
                    retryable=True,
                )

            if not click_seen:
                click_seen = await self.manual_click_seen(decision.attempt_id)
                if click_seen:
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Клик подтверждения обнаружен; проверяем списание баланса",
                        publish,
                    )

            if click_seen:
                balance_before = self._balance_before.get(decision.attempt_id)
                if balance_before is None:
                    raise LivePreparationError(
                        "LIVE_BALANCE_BEFORE_MISSING",
                        "Нет сохранённого баланса перед отправкой ставки.",
                    )

                # Give the header a short moment to receive the bookmaker update.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.35)
                    continue
                except TimeoutError:
                    pass

                if await self.blocked_event_exists(page):
                    await self._log(
                        "LIVE_BLOCKED_EVENT_DETECTED",
                        f"attempt={decision.attempt_id}; phase=after_confirm_click",
                    )
                    return PlacementObservation(
                        False,
                        BLOCKED_EVENT_SIGNAL,
                        retryable=True,
                    )

                balance_after = await self._read_balance(page)
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
                    await self._clear_accepted_coupon(page, decision)
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.BET_PLACED,
                        "Баланс уменьшился на сумму шага; ставка размещена",
                        publish,
                    )
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.ACTIVE,
                        "LIVE-ставка активна",
                        publish,
                    )
                    return PlacementObservation(
                        True,
                        "balance debit confirmed",
                    )

                if not reloaded_after_missing_debit:
                    await self._log(
                        "LIVE_BALANCE_DEBIT_NOT_CONFIRMED",
                        (
                            f"attempt={decision.attempt_id}; before={balance_before}; "
                            f"after={balance_after}; expected_stake={decision.amount}; "
                            "checking lock then reloading once"
                        ),
                    )
                    if await self.blocked_event_exists(page):
                        return PlacementObservation(
                            False,
                            BLOCKED_EVENT_SIGNAL,
                            retryable=True,
                        )
                    reloaded_after_missing_debit = True
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        balance_locator = page.locator(BALANCE_SELECTOR).first
                        await balance_locator.wait_for(
                            state="visible",
                            timeout=15_000,
                        )
                    except Exception as error:
                        await self._log(
                            "LIVE_BALANCE_RELOAD_FAILED",
                            f"attempt={decision.attempt_id}; {type(error).__name__}: {error}",
                        )
                        return None
                    continue

                debit_after_reload = balance_before - balance_after
                await self._log(
                    "LIVE_BALANCE_UNCHANGED_AFTER_RELOAD",
                    (
                        f"attempt={decision.attempt_id}; before={balance_before}; "
                        f"after_reload={balance_after}; debit={debit_after_reload}; "
                        "placement remains unknown; no second click"
                    ),
                )
                return None

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
        self._recovered_blocked_attempts.clear()
        self._balance_before.clear()
