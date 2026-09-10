from __future__ import annotations

import re
from datetime import datetime
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

CENT = Decimal("0.01")
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
    return bool(
        is_zero_zero_score(getattr(match, "score", None))
        and getattr(match, "event_id", None)
        and getattr(match, "url", None)
        and getattr(match, "status", None) != "FINISHED"
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
    """Expose only 0:0 events and put the nearest parseable start time first."""
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


def minimum_second_odds(first_odds: float) -> float:
    """Minimum opposite odd for a two-outcome fork with zero theoretical margin."""
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    if k1 <= 1:
        raise ValueError("Коэффициент первого плеча должен быть больше 1.")
    return float(k1 / (k1 - Decimal("1")))


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
    profitable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_fork(
    first_stake: float,
    first_odds: float,
    second_odds: float,
) -> ForkCalculation:
    """Calculate a cent-rounded hedge and its guaranteed result."""
    s1 = _decimal(first_stake, "Первая ставка")
    k1 = _decimal(first_odds, "Коэффициент первого плеча")
    k2 = _decimal(second_odds, "Коэффициент второго плеча")
    if k1 <= 1 or k2 <= 1:
        raise ValueError("Оба коэффициента должны быть больше 1.")

    threshold = k1 / (k1 - Decimal("1"))
    raw_second = (s1 * k1) / k2
    candidates = {
        raw_second.quantize(CENT, rounding=ROUND_FLOOR),
        raw_second.quantize(CENT, rounding=ROUND_CEILING),
        raw_second.quantize(CENT, rounding=ROUND_HALF_UP),
    }
    candidates = {value for value in candidates if value > 0}

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
        profitable=(k2 > threshold and guaranteed >= 0),
    )


def zero_zero_candidate_payload(match: Any) -> dict[str, Any]:
    result = match.to_dict()
    result.update(
        is_candidate=True,
        target_set=1,
        monitoring_status="ZERO_ZERO_CANDIDATE",
        market_status="WAITING_FOR_MARKET",
        initial_odds_p1=None,
        initial_odds_p2=None,
        current_odds_p1=None,
        current_odds_p2=None,
        odds_history=[],
        first_leg=None,
        second_leg=None,
        minimum_second_odds=None,
        required_second_stake=None,
        zero_fork_capital_required=None,
        fork_percent_preview=None,
        fork=None,
    )
    return result
