"""Bookmaker TRAINING account two-leg Next Goal execution.

Uses the existing verified LIVE coupon placement path; NEVER submits if a
specific bookmaker DEMO-account marker cannot be observed and checked.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Awaitable, Callable
from uuid import uuid4

from backend.app.browser.canvas_2d_adapter import read_next_goal_odds
from backend.app.demo.models import Scorer
from backend.app.live.executor import LiveExecutor

RUBLE = Decimal("1")
Logger = Callable[[str, str], Awaitable[None]]
Save = Callable[[dict[str, Any]], Awaitable[None]]


def whole_ruble(value: Any, *, cap: bool = False) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(RUBLE, rounding=ROUND_DOWN if cap else ROUND_HALF_UP)


def allocate_integer_pair(total: Any, odd1: Any, odd2: Any) -> dict[str, float] | None:
    """Maximize the lower conditional payout after rounding to whole rubles."""
    try:
        amount, a, b = whole_ruble(total, cap=True), Decimal(str(odd1)), Decimal(str(odd2))
    except (ValueError, TypeError, InvalidOperation):
        return None
    if amount < 20 or a <= 1 or b <= 1:
        return None
    inverse = 1 / a + 1 / b
    if inverse >= 1:
        return None
    center = whole_ruble(amount * (1 / a) / inverse)
    best = None
    for delta in (-2, -1, 0, 1, 2):
        stake1 = center + delta
        stake2 = amount - stake1
        if min(stake1, stake2) < 10:
            continue
        payout1 = (stake1 * a).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        payout2 = (stake2 * b).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        floor = min(payout1, payout2)
        if floor <= amount:
            continue
        if best is None or floor > best[0]:
            best = (floor, stake1, stake2, payout1, payout2)
    if not best:
        return None
    _, s1, s2, p1, p2 = best
    return dict(stake1=float(s1), stake2=float(s2), odd1=float(a), odd2=float(b),
                total_stake=float(amount), payout1=float(p1), payout2=float(p2),
                profit1=float(p1-amount), profit2=float(p2-amount),
                minimum_conditional_profit=float(min(p1,p2)-amount),
                no_goal_loss=float(-amount), inverse_sum=float(inverse))


async def require_bookmaker_demo(page: Any) -> str:
    """Use existing RUB authorization check from the main strategy.

    RUB confirms an accessible betting balance, NOT whether that account
    uses real or training funds. The explicit BOOKMAKER_DEMO opt-in is a
    user assertion; there is no invented automatic demo-account detection.
    """
    from auth import check_balance
    if not await check_balance(page):
        raise RuntimeError(
            "RUB-баланс не найден: ставки остановлены. "
            "Проверьте авторизацию и выбранный счёт."
        )
    return "RUB"


def next_second_amount(first_stake: Decimal, first_odd: Any,
                       second_odd: Any, budget: Any) -> Decimal | None:
    """Return a new integer stake only if BOTH conditional legs still cover costs."""
    try:
        a, b, limit = Decimal(str(first_odd)), Decimal(str(second_odd)), whole_ruble(budget, cap=True)
        if min(a,b) <= 1:
            return None
        ideal = whole_ruble(first_stake * a / b)
        options = [ideal + x for x in (-2,-1,0,1,2)]
    except (ValueError, TypeError, InvalidOperation):
        return None
    valid = []
    for second in options:
        total = first_stake + second
        if second < 10 or total > limit:
            continue
        if min(first_stake*a,second*b) <= total:
            continue
        valid.append((min(first_stake*a,second*b)-total,second))
    return max(valid)[1] if valid else None


class DemoPairIncomplete(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


class BookmakerDemoPlacer:
    """Sequential execution, never assumes a click was accepted."""

    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.executor = LiveExecutor(logger)

    async def place(
        self, page: Any, position: dict[str, Any], odds: Any,
        stop: asyncio.Event, save: Save,
        read_score: Callable[[Any], Awaitable[Any]],
        in_window: Callable[[Any], bool],
    ) -> bool:
        from backend.app.live.models import LiveDecision
        first = 1 if odds.team1 <= odds.team2 else 2
        second = 3 - first
        position.update(
            placement_order=[first, second], accepted_legs=0,
            bookmaker_settled=False,
            team1_placement="PLANNED", team2_placement="PLANNED",
        )
        await save(position)

        async def send(side: int, stake: Decimal, fresh_odds: Any) -> None:
            await require_bookmaker_demo(page)
            if stop.is_set():
                raise DemoPairIncomplete("Наблюдение остановлено до ставки")
            snap = await read_score(page)
            if not in_window(snap) or snap.score.text() != position["score_before"]:
                raise DemoPairIncomplete("Время/счёт изменились до клика по коэффициенту")
            key = f"team{side}_placement"
            if position[key] != "PLANNED":
                raise DemoPairIncomplete("Эта ставка уже пыталась отправиться", uncertain=True)
            attempt = f'demo-fork-{position["id"]}-{side}-{uuid4().hex[:6]}'
            position.update({key:"SUBMITTING",f"team{side}_attempt_id":attempt})
            await save(position)
            decision = LiveDecision(
                attempt_id=attempt, match_id=position["match_id"],
                team=position[f"team{side}"],
                side=Scorer.TEAM_1 if side == 1 else Scorer.TEAM_2,
                strategy_step=1, amount=float(stake), goal_number=position["goal_number"],
                coefficient=float(fresh_odds.team1 if side == 1 else fresh_odds.team2),
                coefficient_locator=fresh_odds.locator_for_side(
                    Scorer.TEAM_1 if side == 1 else Scorer.TEAM_2
                ),
            )
            try:
                await self.executor.prepare(page, decision)
                clicked = await self.executor.manual_click_seen(attempt)
                if not clicked:
                    position[key] = "NOT_ACCEPTED"
                    await save(position)
                    await self.executor.clear_unaccepted_coupon(page, attempt)
                    raise DemoPairIncomplete(f"Купон стороны {side} не отправлен")
                result = await self.executor.wait_for_manual_confirmation(
                    page, decision, stop,
                )
                if result is None:
                    raise DemoPairIncomplete(
                        f"Принятие купона стороны {side} не подтверждено",
                        uncertain=True,
                    )
                if not result.placed:
                    position[key] = "NOT_ACCEPTED"
                    await save(position)
                    raise DemoPairIncomplete(f"Купон стороны {side} отклонён: {result.signal}")
                position[key] = "ACCEPTED"
                position[f"team{side}_accepted_odd"] = decision.coefficient
                position[f"team{side}_accepted_stake"] = float(stake)
                position["accepted_legs"] += 1
                await save(position)
                await self.logger(
                    "DEMO_BOOKMAKER_LEG_ACCEPTED",
                    f"Принята ставка на команду {side}: {stake} ₽ @ {decision.coefficient}",
                )
                if await self.executor._success_modal_is_visible(page):
                    if not await self.executor._close_success_modal_controls(page, attempt):
                        raise DemoPairIncomplete(
                            "Модальное окно осталось открытым — вторая ставка запрещена"
                        )
            except DemoPairIncomplete:
                raise
            except Exception as error:
                clicked = await self.executor.manual_click_seen(attempt)
                if position[key] == "SUBMITTING":
                    position[key] = "UNKNOWN" if clicked else "NOT_ACCEPTED"
                    await save(position)
                raise DemoPairIncomplete(
                    f"Не удалось проверить сторону {side}: {error}", uncertain=clicked
                ) from error

        try:
            await require_bookmaker_demo(page)
            if (await self.executor._read_balance(page)) < Decimal(str(position["total_stake"])):
                raise DemoPairIncomplete("Демо-баланса недостаточно на обе ставки")
            amount_first = whole_ruble(position[f"stake{first}"])
            await send(first, amount_first, odds)
            await require_bookmaker_demo(page)
            latest_score = await read_score(page)
            if not in_window(latest_score) or latest_score.score.text() != position["score_before"]:
                raise DemoPairIncomplete("Окно/счёт изменились после первой принятой ставки")
            newer = await read_next_goal_odds(
                page, latest_score.team1, latest_score.team2,
                latest_score.score.team1, latest_score.score.team2,
                self.logger, read_only=True,
            )
            if (newer.next_goal_number != position["goal_number"]
                    or newer.locked_sides or newer.recent_locked_sides):
                raise DemoPairIncomplete("Второй исход заблокирован или рынок уже другой")
            odd_first = Decimal(str(odds.team1 if first == 1 else odds.team2))
            odd_second = newer.team1 if second == 1 else newer.team2
            amount_second = next_second_amount(
                amount_first, odd_first, odd_second, position["total_stake"],
            )
            if amount_second is None:
                raise DemoPairIncomplete(
                    "После первой ставки коэффициент изменился: нет прибыльной "
                    "пары в лимите бюджета"
                )
            position[f"stake{second}"] = float(amount_second)
            position[f"odd{second}"] = float(odd_second)
            total = amount_first + amount_second
            position["total_stake"] = float(total)
            position["no_goal_loss"] = float(-total)
            for index in (1,2):
                payout = (Decimal(str(position[f"stake{index}"])) *
                          Decimal(str(position[f"odd{index}"]))).quantize(
                              Decimal(".01"),rounding=ROUND_DOWN,
                          )
                position[f"payout{index}"] = float(payout)
                position[f"profit{index}"] = float(payout-total)
            position["minimum_conditional_profit"] = min(
                position["profit1"],position["profit2"]
            )
            await save(position)
            await send(second, amount_second, newer)
            return True
        except DemoPairIncomplete as error:
            position["status"] = (
                "BOOKMAKER_DEMO_SUBMISSION_UNKNOWN" if error.uncertain
                else "BOOKMAKER_DEMO_PARTIAL" if position["accepted_legs"]
                else "BOOKMAKER_DEMO_REJECTED"
            )
            position["placement_error"] = str(error)
            await save(position)
            await self.logger("BOOKMAKER_DEMO_HALT", str(error))
            return False
