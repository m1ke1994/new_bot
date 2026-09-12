from __future__ import annotations

import re
from datetime import datetime
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

CENT = Decimal("0.01")
MIN_ARB_PERCENT = 0.5
DEFAULT_MINIMUM_STAKE = 1.0
ROUNDING_TOLERANCE = 0.01
ODDS_EQUAL_EPSILON = 0.005
ZERO_SCORE_RE = re.compile(r"^\s*0+\s*[:\-–—]\s*0+\s*$")


def _decimal(value: float | int | str | Decimal, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{name} должен быть числом.") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} должен быть больше нуля.")
    return result


def is_zero_zero_score(score: str | None) -> bool:
    return bool(score and ZERO_SCORE_RE.fullmatch(str(score)))


def is_zero_zero_match(match: Any) -> bool:
    """Keep only already started LIVE matches whose current score is exactly 0:0."""
    return bool(
        is_zero_zero_score(getattr(match, "score", None))
        and getattr(match, "event_id", None)
        and getattr(match, "url", None)
        and getattr(match, "status", None) == "LIVE"
        and getattr(match, "started", None) is True
    )


def _time_distance_minutes(value: str | None, now_minutes: int) -> int | None:
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", str(value or ""))
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return (hour * 60 + minute - now_minutes) % (24 * 60)


def select_zero_zero_matches(matches: list[Any]) -> list[Any]:
    """Expose only active LIVE 0:0 events and keep scanner order stable when time is absent."""
    filtered = [match for match in matches if is_zero_zero_match(match)]
    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    indexed = list(enumerate(filtered))

    def key(item: tuple[int, Any]) -> tuple[int, int, int]:
        index, match = item
        distance = _time_distance_minutes(getattr(match, "time", None), now_minutes)
        if distance is None:
            return (1, index, index)
        return (0, distance, index)

    return [match for _, match in sorted(indexed, key=key)]


def calculate_zero_hedge_odd(first_odds: float) -> float:
    """Minimum opposite odd for a two-outcome fork with zero theoretical margin."""
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    if k1 <= 1:
        raise ValueError("Коэффициент первого плеча должен быть больше 1.")
    return float(k1 / (k1 - Decimal("1")))


def minimum_second_odds(first_odds: float) -> float:
    """Backward-compatible name for the zero-profit hedge boundary."""
    return calculate_zero_hedge_odd(first_odds)


def calculate_arbitrage_percent(first_odds: float, opposite_odds: float) -> float:
    """Return the two-way arbitrage margin from the accepted and live odds."""
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    k2 = _decimal(opposite_odds, "Коэффициент второго плеча")
    if k1 <= 1 or k2 <= 1:
        raise ValueError("Оба коэффициента должны быть больше 1.")
    result = (Decimal("1") - (Decimal("1") / k1 + Decimal("1") / k2)) * Decimal("100")
    return float(result)


def is_arbitrage_available(
    first_odds: float,
    opposite_odds: float,
    min_arb_percent: float = MIN_ARB_PERCENT,
) -> bool:
    """Require a positive fork and the configured safety margin."""
    minimum = Decimal(str(min_arb_percent))
    if minimum < 0:
        raise ValueError("Минимальный процент вилки не может быть отрицательным.")
    current = Decimal(str(calculate_arbitrage_percent(first_odds, opposite_odds)))
    return current > 0 and current >= minimum


def select_favorite(
    odd_1: float,
    odd_2: float,
    *,
    epsilon: float = ODDS_EQUAL_EPSILON,
) -> str:
    """Choose the player with the lower odd; equal odds have no unique favorite."""
    first = _decimal(odd_1, "Коэффициент игрока 1")
    second = _decimal(odd_2, "Коэффициент игрока 2")
    tolerance = Decimal(str(epsilon))
    if tolerance < 0:
        raise ValueError("Точность сравнения коэффициентов не может быть отрицательной.")
    if abs(first - second) < tolerance:
        raise ValueError("При равных коэффициентах фаворит не определён.")
    return "p1" if first < second else "p2"


def calculate_hedge_stake(
    first_stake: float,
    first_odds: float,
    second_odds: float,
    *,
    minimum_stake: float = DEFAULT_MINIMUM_STAKE,
) -> float:
    """Balance both payouts and round the paper stake to bookmaker cents."""
    s1 = _decimal(first_stake, "Первая ставка")
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    k2 = _decimal(second_odds, "Коэффициент второго плеча")
    site_minimum = _decimal(minimum_stake, "Минимальная ставка")
    raw_second = (s1 * k1) / k2
    rounded = raw_second.quantize(CENT, rounding=ROUND_HALF_UP)
    return float(max(rounded, site_minimum))


def calculate_outcome_profit(
    first_stake: float,
    first_odds: float,
    second_stake: float,
    second_odds: float,
) -> tuple[float, float]:
    """Return net profit if the first or the hedge player wins."""
    s1 = _decimal(first_stake, "Первая ставка")
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    s2 = _decimal(second_stake, "Вторая ставка")
    k2 = _decimal(second_odds, "Коэффициент второго плеча")
    total = s1 + s2
    first = (s1 * k1 - total).quantize(CENT, rounding=ROUND_HALF_UP)
    second = (s2 * k2 - total).quantize(CENT, rounding=ROUND_HALF_UP)
    return float(first), float(second)


def zero_fork_capital_required(first_stake: float, first_odds: float) -> float:
    """Capital required to fund both legs when the second odd is exactly break-even."""
    stake = _decimal(first_stake, "Первая ставка")
    odds = _decimal(first_odds, "Коэффициент первого плеча")
    if odds <= 1:
        raise ValueError("Коэффициент первого плеча должен быть больше 1.")
    return float((stake * odds).quantize(CENT, rounding=ROUND_CEILING))


@dataclass(frozen=True)
class ForkCalculation:
    first_stake: float
    first_odds: float
    second_stake: float
    second_odds: float
    minimum_second_odds: float
    total_stake: float
    payout_if_first: float
    payout_if_second: float
    profit_if_first: float
    profit_if_second: float
    guaranteed_profit: float
    fork_percent: float
    arbitrage_percent: float
    profitable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SettlementCalculation:
    balance_before: float
    total_invested: float
    payout: float
    balance_after: float
    profit_loss: float
    winner: str
    winning_leg: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_series_settlement(
    balance_before: float,
    first_side: str,
    first_stake: float,
    first_odds: float,
    winner: str,
    *,
    hedge_side: str | None = None,
    hedge_stake: float | None = None,
    hedge_odds: float | None = None,
) -> SettlementCalculation:
    """Settle one paper series, including a legitimate first-leg-only loss."""
    before = _decimal(balance_before, "Баланс до серии")
    s1 = _decimal(first_stake, "Первая ставка")
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    if first_side not in {"p1", "p2"} or winner not in {"p1", "p2"}:
        raise ValueError("Сторона ставки и победитель должны быть p1 или p2.")

    second: tuple[Decimal, Decimal] | None = None
    if hedge_side is not None or hedge_stake is not None or hedge_odds is not None:
        if hedge_side not in {"p1", "p2"} or hedge_side == first_side:
            raise ValueError("Второе плечо должно быть на противоположного игрока.")
        second = (
            _decimal(hedge_stake, "Вторая ставка"),
            _decimal(hedge_odds, "Коэффициент второго плеча"),
        )

    total = s1 + (second[0] if second else Decimal("0"))
    payout = Decimal("0")
    winning_leg: str | None = None
    if winner == first_side:
        payout = s1 * k1
        winning_leg = "FIRST_LEG"
    elif second is not None and winner == hedge_side:
        payout = second[0] * second[1]
        winning_leg = "HEDGE_LEG"

    after = before - total + payout
    pnl = after - before

    def q(value: Decimal) -> float:
        return float(value.quantize(CENT, rounding=ROUND_HALF_UP))

    return SettlementCalculation(
        balance_before=q(before),
        total_invested=q(total),
        payout=q(payout),
        balance_after=q(after),
        profit_loss=q(pnl),
        winner=winner,
        winning_leg=winning_leg,
    )


def calculate_fork(
    first_stake: float,
    first_odds: float,
    second_odds: float,
    *,
    min_arb_percent: float = MIN_ARB_PERCENT,
    minimum_stake: float = DEFAULT_MINIMUM_STAKE,
    rounding_tolerance: float = ROUNDING_TOLERANCE,
) -> ForkCalculation:
    """Calculate a cent-rounded hedge and its guaranteed result."""
    s1 = _decimal(first_stake, "Первая ставка")
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    k2 = _decimal(second_odds, "Коэффициент второго плеча")
    if k1 <= 1 or k2 <= 1:
        raise ValueError("Оба коэффициента должны быть больше 1.")

    threshold = k1 / (k1 - Decimal("1"))
    site_minimum = _decimal(minimum_stake, "Минимальная ставка")
    tolerance = Decimal(str(rounding_tolerance))
    if tolerance < 0:
        raise ValueError("Допуск округления не может быть отрицательным.")
    raw_second = (s1 * k1) / k2
    candidates = {
        raw_second.quantize(CENT, rounding=ROUND_FLOOR),
        raw_second.quantize(CENT, rounding=ROUND_CEILING),
        raw_second.quantize(CENT, rounding=ROUND_HALF_UP),
    }
    candidates = {max(value, site_minimum) for value in candidates if value > 0}

    def metrics(s2: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        total = s1 + s2
        payout_first = s1 * k1
        payout_second = s2 * k2
        profit_first = payout_first - total
        profit_second = payout_second - total
        return total, payout_first, payout_second, profit_first, profit_second

    best_second = max(
        candidates,
        key=lambda value: (
            min(metrics(value)[3], metrics(value)[4]),
            -abs(value - raw_second),
        ),
    )
    total, payout_first, payout_second, profit_first, profit_second = metrics(best_second)
    guaranteed = min(profit_first, profit_second)
    percent = (guaranteed / total * Decimal("100")) if total else Decimal("0")
    arbitrage_percent = calculate_arbitrage_percent(k1, k2)

    def q(value: Decimal) -> float:
        return float(value.quantize(CENT, rounding=ROUND_HALF_UP))

    return ForkCalculation(
        first_stake=q(s1),
        first_odds=float(k1),
        second_stake=q(best_second),
        second_odds=float(k2),
        minimum_second_odds=float(threshold),
        total_stake=q(total),
        payout_if_first=q(payout_first),
        payout_if_second=q(payout_second),
        profit_if_first=q(profit_first),
        profit_if_second=q(profit_second),
        guaranteed_profit=q(guaranteed),
        fork_percent=float(percent.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
        arbitrage_percent=float(
            Decimal(str(arbitrage_percent)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        ),
        profitable=(
            is_arbitrage_available(k1, k2, min_arb_percent)
            and profit_first >= -tolerance
            and profit_second >= -tolerance
        ),
    )


def zero_zero_candidate_payload(match: Any) -> dict[str, Any]:
    result = match.to_dict()
    result.update(
        is_candidate=True,
        target_set=2,
        party=2,
        monitoring_status="WAITING_SECOND_SET",
        market_status="WAITING_FOR_MARKET",
        initial_odds_p1=None,
        initial_odds_p2=None,
        current_odds_p1=None,
        current_odds_p2=None,
        odds_history=[],
        first_leg=None,
        second_leg=None,
        first_bet_player=None,
        first_bet_odd=None,
        first_bet_amount=None,
        opposite_player=None,
        hedge_player=None,
        sequence_id=None,
        minimum_second_odds=None,
        required_second_stake=None,
        zero_fork_capital_required=None,
        fork_percent_preview=None,
        arbitrage_percent_preview=None,
        fork=None,
    )
    return result
