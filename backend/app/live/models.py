from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from backend.app.demo.models import Score, Scorer


class LiveStatus(StrEnum):
    IDLE = "IDLE"
    WAITING_FOR_MARKET = "WAITING_FOR_MARKET"
    LIVE_MARKET_SELECTED = "LIVE_MARKET_SELECTED"
    LIVE_COUPON_OPENED = "LIVE_COUPON_OPENED"
    LIVE_AMOUNT_FILLED = "LIVE_AMOUNT_FILLED"
    LIVE_AMOUNT_VERIFIED = "LIVE_AMOUNT_VERIFIED"
    READY_FOR_MANUAL_CONFIRMATION = "READY_FOR_MANUAL_CONFIRMATION"
    AWAITING_PLACEMENT_RESULT = "AWAITING_PLACEMENT_RESULT"
    STALE_COUPON = "STALE_COUPON"
    BET_PLACED = "BET_PLACED"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PendingLiveBet:
    decision_id: str
    match_id: str
    team: str
    side: Scorer
    strategy_step: int
    amount: float
    target_goal_number: int

    def with_score(self, score: Score) -> "PendingLiveBet":
        return replace(self, target_goal_number=score.team1 + score.team2 + 1)


@dataclass(frozen=True)
class ActiveLiveBet:
    attempt_id: str
    match_id: str
    team: str
    side: Scorer
    strategy_step: int
    amount: float
    coefficient: float
    goal_number: int
    score_before: Score


@dataclass(frozen=True)
class LiveDecision:
    attempt_id: str
    match_id: str
    team: str
    side: Scorer
    strategy_step: int
    amount: float
    goal_number: int
    coefficient: float
    coefficient_locator: Any


@dataclass(frozen=True)
class PlacementObservation:
    placed: bool
    signal: str
    retryable: bool = False


class LivePreparationError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
