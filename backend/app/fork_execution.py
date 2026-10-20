"""FC25 fork LIVE execution using the battle-tested main LIVE coupon executor.

Two NEXT-GOAL team outcomes form only a CONDITIONAL hedge: the 'no more goals'
outcome is uncovered, and the two bookmaker orders are inherently non-atomic.
There are no blind retries after an attempted real-money submit.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Awaitable, Callable
from uuid import uuid4

from backend.app.browser.canvas_2d_adapter import read_next_goal_odds
from backend.app.demo.models import Scorer
from backend.app.live.executor import LiveExecutor
from backend.app.live.models import LiveDecision, LivePreparationError

RUBLE = Decimal("1")
MIN_STAKE = Decimal("10")
Logger = Callable[[str, str], Awaitable[None]]
Save = Callable[[dict[str, Any]], Awaitable[None]]


def integer_rubles(value: Any) -> Decimal:
    """Normal nearest whole ruble; no floating-point rounding artifacts."""
    return Decimal(str(value)).quantize(RUBLE, rounding=ROUND_HALF_UP)


def budget_rubles(value: Any) -> Decimal:
    """Never round a budget UP and exceed the entered limit."""
    return Decimal(str(value)).quantize(RUBLE, rounding=ROUND_DOWN)


def integer_pair(total: Any, odd1: Any, odd2: Any) -> dict[str, float] | None:
    """Allocate integer rubles, maximizing worst-case *conditional* payout."""
    try:
        a, b, amount = Decimal(str(odd1)), Decimal(str(odd2)), budget_rubles(total)
    except (ValueError, ArithmeticError):
        return None
    if amount < 2 * MIN_STAKE or a <= 1 or b <= 1:
        return None
    ratio = (Decimal(1) / a) / ((Decimal(1) / a) + (Decimal(1) / b))
    middle = integer_rubles(amount * ratio)
    best: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
    for delta in (-2, -1, 0, 1, 2):
        first = middle + delta
        second = amount - first
        if min(first, second) < MIN_STAKE:
            continue
        payout1 = (first * a).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        payout2 = (second * b).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        if min(payout1, payout2) <= amount:
            continue
        result = (min(payout1, payout2), first, payout1, payout2)
        if best is None or result[0] > best[0]:
            best = result
    if best is None:
        return None
    _, first, payout1, payout2 = best
    second = amount - first
    return {
        "odd1": float(a), "odd2": float(b),
        "stake1": float(first), "stake2": float(second),
        "total_stake": float(amount),
        "payout1": float(payout1), "payout2": float(payout2),
        "profit1": float(payout1 - amount),
        "profit2": float(payout2 - amount),
        "minimum_conditional_profit": float(min(payout1, payout2) - amount),
        "no_goal_loss": -float(amount),
        "inverse_sum": float(Decimal(1) / a + Decimal(1) / b),
    }


def reprice_second(first_amount: Any, first_odd: Any, second_odd: Any, max_total: Any) -> Decimal | None:
    """After acceptance of leg 1, size leg 2 against its FRESH quoted price."""
    try:
        first = integer_rubles(first_amount)
        a, b = Decimal(str(first_odd)), Decimal(str(second_odd))
        limit = budget_rubles(max_total)
    except (ArithmeticError, ValueError):
        return None
    if a <= 1 or b <= 1 or first < MIN_STAKE:
        return None
    second = integer_rubles(first * a / b)
    total = first + second
    if second < MIN_STAKE or total > limit:
        return None
    if min(first * a, second * b) <= total:
        return None
    return second


class ForkLiveIncomplete(RuntimeError):
    """No second real bet is ever placed when first/second is uncertain."""
    def __init__(self, message: str, *, accepted: int, uncertain: bool = False):
        super().__init__(message)
        self.accepted = accepted
        self.uncertain = uncertain


class ForkLivePlacer:
    def __init__(self, logger: Logger):
        self.executor = LiveExecutor(logger)
        self.logger = logger

    async def _leg(
        self,
        page: Any,
        position: dict[str, Any],
        odds: Any,
        side: int,
        amount: Decimal,
        stop_event: asyncio.Event,
        save: Save,
        *,
        deadline: float,
    ) -> None:
        key = f"team{side}_placement"
        if position.get(key) not in (None, "PLANNED"):
            raise ForkLiveIncomplete("Попытка по этой команде уже записана", accepted=sum(
                position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)
            ), uncertain=True)
        attempt_id = f'fork-{position["id"]}-{side}-{uuid4().hex[:8]}'
        position[key] = "SUBMITTING"
        position[f"team{side}_attempt_id"] = attempt_id
        position[f"team{side}_amount"] = float(amount)
        await save(position)
        decision = LiveDecision(
            attempt_id=attempt_id,
            match_id=position["match_id"],
            team=position[f"team{side}"],
            side=Scorer.TEAM_1 if side == 1 else Scorer.TEAM_2,
            strategy_step=1,
            amount=float(amount),
            goal_number=position["goal_number"],
            coefficient=float(odds.team1 if side == 1 else odds.team2),
            coefficient_locator=odds.locator_for_side(Scorer.TEAM_1 if side == 1 else Scorer.TEAM_2),
            submission_deadline_monotonic=deadline,
        )
        await self.logger(
            "LIVE_FORK_LEG_START",
            f'Сторона {side}; сумма {amount} ₽ (целые рубли); '
            f'котировка {decision.coefficient}; попытка {attempt_id}',
        )
        try:
            await self.executor.prepare(page, decision)
            clicked = await self.executor.manual_click_seen(attempt_id)
            if not clicked:
                position[key] = "NOT_ACCEPTED"
                await save(position)
                await self.executor.clear_unaccepted_coupon(page, attempt_id)
                raise ForkLiveIncomplete(
                    "Кнопка «Сделать ставку» не была нажата, купон не принят",
                    accepted=sum(position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)),
                )
            result = await self.executor.wait_for_manual_confirmation(
                page, decision, stop_event,
            )
            if result is None:
                raise ForkLiveIncomplete(
                    "После отправки купона не удалось достоверно подтвердить принятие",
                    accepted=sum(position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)),
                    uncertain=True,
                )
            if not result.placed:
                position[key] = "NOT_ACCEPTED"
                await save(position)
                raise ForkLiveIncomplete(
                    f"Купон не принят: {result.signal}",
                    accepted=sum(position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)),
                )
            position[key] = "ACCEPTED"
            position[f"team{side}_accepted_odd"] = float(decision.coefficient)
            position[f"team{side}_acceptance_signal"] = result.signal
            await save(position)
            await self.logger(
                "LIVE_FORK_LEG_ACCEPTED",
                f"Ставка по стороне {side} подтверждена: {result.signal}",
            )
            # The shared executor already attempts Continue/X after success,
            # but demand a genuinely dismissed modal before the next market click.
            if await self.executor._success_modal_is_visible(page):
                dismissed = await self.executor._close_success_modal_controls(
                    page, attempt_id,
                )
                if not dismissed or await self.executor._success_modal_is_visible(page):
                    raise ForkLiveIncomplete(
                        "Окно принятой ставки осталось открытым — вторая не отправлена",
                        accepted=sum(position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)),
                    )
        except ForkLiveIncomplete:
            raise
        except Exception as exc:
            submitted = await self.executor.manual_click_seen(attempt_id)
            if position.get(key) == "SUBMITTING":
                position[key] = "UNKNOWN" if submitted else "NOT_ACCEPTED"
                await save(position)
            raise ForkLiveIncomplete(
                f"Ошибка реальной ставки по стороне {side}: {type(exc).__name__}: {exc}",
                accepted=sum(position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)),
                uncertain=submitted,
            ) from exc

    async def execute(
        self,
        page: Any,
        position: dict[str, Any],
        odds: Any,
        stop_event: asyncio.Event,
        save: Save,
        read_scoreboard: Callable[[Any], Awaitable[Any]],
        logger: Logger,
        *,
        close_seconds: int,
        clock_to_seconds: Callable[[str], int | None],
    ) -> bool:
        """One submission attempt per side; never infer two accepted bets from clicks."""
        first = 1 if odds.team1 <= odds.team2 else 2
        second = 2 if first == 1 else 1
        position.update(
            placement_order=[first, second], mode="LIVE",
            team1_placement="PLANNED", team2_placement="PLANNED",
            bookmaker_settled=False, accepted_legs=0,
        )
        await save(position)
        amount_first = integer_rubles(position[f"stake{first}"])
        deadline = asyncio.get_running_loop().time() + 90
        try:
            balance = await self.executor._read_balance(page)
            if balance < Decimal(str(position["total_stake"])):
                raise ForkLiveIncomplete(
                    f"Недостаточный подтверждённый RUB-баланс {balance}, "
                    f'планируемая сумма {position["total_stake"]}',
                    accepted=0,
                )
            if stop_event.is_set():
                raise ForkLiveIncomplete("Получен сигнал остановки до LIVE-ставки", accepted=0)
            await self._leg(page, position, odds, first, amount_first,
                            stop_event, save, deadline=deadline)
            position["accepted_legs"] = 1
            await save(position)

            fresh_score = await read_scoreboard(page)
            if (
                fresh_score.score.text() != position["score_before"]
                or clock_to_seconds(fresh_score.timer) is None
                or clock_to_seconds(fresh_score.timer) > close_seconds
            ):
                raise ForkLiveIncomplete(
                    "Счёт или время изменились после принятия первой ставки. "
                    "Вторая ставка запрещена.",
                    accepted=1,
                )
            if stop_event.is_set():
                raise ForkLiveIncomplete("Остановка после первой принятой ставки", accepted=1)
            fresh = await read_next_goal_odds(
                page, fresh_score.team1, fresh_score.team2,
                fresh_score.score.team1, fresh_score.score.team2,
                logger, read_only=True,
            )
            if (
                fresh.next_goal_number != position["goal_number"]
                or fresh.locked_sides or fresh.recent_locked_sides
            ):
                raise ForkLiveIncomplete(
                    "Рынок или противоположный исход изменён/заблокирован после первой ставки",
                    accepted=1,
                )
            second_odd = fresh.team1 if second == 1 else fresh.team2
            first_odd = odds.team1 if first == 1 else odds.team2
            second_amount = reprice_second(
                amount_first, first_odd, second_odd,
                position["total_stake"],
            )
            if second_amount is None:
                raise ForkLiveIncomplete(
                    "Новые коэффициенты не позволяют покрыть первую ставку "
                    "в пределах заданного бюджета и с положительным "
                    "условным результатом",
                    accepted=1,
                )
            # Rewrite calculated stakes to accepted+new proposed quote, not stale values.
            position[f"stake{second}"] = float(second_amount)
            position[f"odd{second}"] = float(second_odd)
            total = amount_first + second_amount
            position["total_stake"] = float(total)
            position["no_goal_loss"] = -float(total)
            for i in (1, 2):
                payout = (Decimal(str(position[f"stake{i}"])) *
                          Decimal(str(position[f"odd{i}"]))).quantize(
                              Decimal(".01"), rounding=ROUND_DOWN,
                          )
                position[f"payout{i}"] = float(payout)
                position[f"profit{i}"] = float(payout - total)
            position["minimum_conditional_profit"] = float(min(
                Decimal(str(position["profit1"])),
                Decimal(str(position["profit2"])),
            ))
            await save(position)
            await self._leg(page, position, fresh, second, second_amount,
                            stop_event, save, deadline=deadline)
            position["accepted_legs"] = 2
            await save(position)
            return True
        except ForkLiveIncomplete as exc:
            position["accepted_legs"] = sum(
                position.get(f"team{i}_placement") == "ACCEPTED" for i in (1, 2)
            )
            position["status"] = "LIVE_PARTIAL" if position["accepted_legs"] else (
                "LIVE_SUBMISSION_UNKNOWN" if exc.uncertain else "LIVE_REJECTED"
            )
            if exc.uncertain:
                position["status"] = "LIVE_SUBMISSION_UNKNOWN"
            position["live_error"] = str(exc)
            await save(position)
            await logger("LIVE_FORK_STOPPED", str(exc))
            return False
