import asyncio
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import LiveDecision, LivePreparationError, LiveStatus, PlacementObservation


Logger = Callable[[str, str], Awaitable[Any]]
Publisher = Callable[[LiveStatus, str], Awaitable[Any]]

COUPON_SELECTOR = ".quick-coupon-main"
ACCOUNT_SELECTOR = '.quick-coupon-main button[aria-label="Основной (RUB)"]'
AMOUNT_SELECTOR = (
    '.quick-coupon-main input.ui-number-input__field[placeholder="Введите сумму ставки"]'
)
CONFIRM_SELECTOR = ".quick-coupon-main button.quick-coupon-put-bet-button"
CONFIRM_TEXT = "Сделать ставку"

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
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_MARKET_SELECTED,
                    f"goal={decision.goal_number} team={decision.side.value} odds={decision.coefficient}",
                    publish,
                )

                coupon = page.locator(COUPON_SELECTOR).first
                await coupon.wait_for(state="visible", timeout=10_000)
                await self._publish(
                    decision.attempt_id,
                    LiveStatus.LIVE_COUPON_OPENED,
                    "Quick coupon открыт",
                    publish,
                )

                account = page.locator(ACCOUNT_SELECTOR).first
                if await account.count() == 0 or not await account.is_visible():
                    raise LivePreparationError(
                        "LIVE_RUB_ACCOUNT_NOT_FOUND", "Счёт «Основной (RUB)» не найден в coupon."
                    )
                pressed = await account.get_attribute("aria-pressed")
                if pressed is not None and pressed.lower() == "false":
                    raise LivePreparationError(
                        "LIVE_RUB_ACCOUNT_NOT_SELECTED", "Счёт «Основной (RUB)» не выбран."
                    )

                amount_input = page.locator(AMOUNT_SELECTOR).first
                if await amount_input.count() == 0 or not await amount_input.is_visible():
                    raise LivePreparationError(
                        "LIVE_AMOUNT_INPUT_NOT_FOUND", "Поле суммы в quick coupon не найдено."
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

                confirm = page.locator(CONFIRM_SELECTOR).first
                if await confirm.count() == 0 or not await confirm.is_visible():
                    raise LivePreparationError(
                        "LIVE_CONFIRM_BUTTON_NOT_FOUND", "Кнопка «Сделать ставку» не найдена."
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

        # Единственный критерий успешной отправки:
        # после клика одновременно исчезли:
        # 1) input суммы;
        # 2) кнопка «Сделать ставку».
        #
        # Никакие toast/modal/coupon-wrapper больше не используются
        # для подтверждения размещения.
        amount_input = page.locator(AMOUNT_SELECTOR).first
        confirm = page.locator(CONFIRM_SELECTOR).first

        click_seen = current_state == LiveStatus.AWAITING_PLACEMENT_RESULT

        while not stop_event.is_set():
            if not click_seen:
                click_seen = await self.manual_click_seen(decision.attempt_id)
                if click_seen:
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.AWAITING_PLACEMENT_RESULT,
                        "Клик подтверждения обнаружен; ждём исчезновение кнопки и поля суммы",
                        publish,
                    )

            if click_seen:
                try:
                    confirm_visible = (
                        await confirm.count() > 0
                        and await confirm.is_visible()
                    )
                except Exception:
                    confirm_visible = False

                try:
                    amount_visible = (
                        await amount_input.count() > 0
                        and await amount_input.is_visible()
                    )
                except Exception:
                    amount_visible = False

                await self._log(
                    "LIVE_PLACEMENT_CHECK",
                    (
                        f"attempt={decision.attempt_id}; "
                        f"confirm_visible={confirm_visible}; "
                        f"amount_visible={amount_visible}"
                    ),
                )

                # ГЛАВНЫЙ И ЕДИНСТВЕННЫЙ ТРИГГЕР:
                # оба элемента исчезли -> ставка считается размещённой.
                if not confirm_visible and not amount_visible:
                    await self._publish(
                        decision.attempt_id,
                        LiveStatus.BET_PLACED,
                        "Кнопка «Сделать ставку» и поле суммы исчезли; ставка размещена",
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
                        "confirm button and amount input disappeared",
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
