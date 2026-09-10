from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import StrEnum
from typing import Any

from .models import NextGoalOdds, Score, Scorer, TeamSelection


MONEY_QUANTUM = Decimal("0.01")


class StrategyType(StrEnum):
    NEXT_GOAL = "NEXT_GOAL"
    TOTAL_EVEN = "TOTAL_EVEN"

    @property
    def display_name(self) -> str:
        return {
            self.NEXT_GOAL: "Следующий гол",
            self.TOTAL_EVEN: "Тотал чёт",
        }[self]


def money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class StrategyConfig:
    """Saved stake row. ``stakes`` is always the execution source of truth."""

    initial_stake: Decimal
    progression_multiplier: Decimal
    max_steps: int
    stakes: tuple[Decimal, ...]
    exclude_teams_enabled: bool
    min_initial_odds_enabled: bool
    strategy_type: StrategyType

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "StrategyConfig":
        initial_stake = money(payload.get("initial_stake", 20))
        multiplier = Decimal(str(payload.get("progression_multiplier", "2.2")))
        max_steps = int(payload.get("max_steps", 7))
        raw_stakes = payload.get("stakes")
        stakes = tuple(money(value) for value in raw_stakes) if raw_stakes is not None else generate_stakes(initial_stake, multiplier, max_steps)
        exclude_teams_enabled = _boolean_setting(
            payload, "exclude_teams_enabled", default=True
        )
        min_initial_odds_enabled = _boolean_setting(
            payload, "min_initial_odds_enabled", default=True
        )
        try:
            strategy_type = StrategyType(
                str(payload.get("strategy_type", StrategyType.NEXT_GOAL.value)).strip().upper()
            )
        except ValueError as error:
            raise ValueError("strategy_type must be NEXT_GOAL or TOTAL_EVEN") from error
        config = cls(
            initial_stake,
            multiplier,
            max_steps,
            stakes,
            exclude_teams_enabled,
            min_initial_odds_enabled,
            strategy_type,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.initial_stake <= 0:
            raise ValueError("initial_stake must be greater than zero")
        if self.progression_multiplier <= 1:
            raise ValueError("progression_multiplier must be greater than one")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least one")
        if len(self.stakes) != self.max_steps or any(value <= 0 for value in self.stakes):
            raise ValueError("stakes must contain one positive amount per step")

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_stake": float(self.initial_stake),
            "progression_multiplier": float(self.progression_multiplier),
            "max_steps": self.max_steps,
            "stakes": [float(value) for value in self.stakes],
            "required_budget": float(sum(self.stakes, Decimal("0"))),
            "exclude_teams_enabled": self.exclude_teams_enabled,
            "min_initial_odds_enabled": self.min_initial_odds_enabled,
            "strategy_type": self.strategy_type.value,
        }


def _boolean_setting(
    payload: dict[str, Any], key: str, *, default: bool
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def generate_stakes(initial_stake: Decimal | float | int | str, progression_multiplier: Decimal | float | int | str, max_steps: int) -> tuple[Decimal, ...]:
    initial = money(initial_stake)
    multiplier = Decimal(str(progression_multiplier))
    if initial <= 0 or multiplier <= 1 or max_steps < 1:
        raise ValueError("Invalid strategy configuration")
    result = [initial]
    for _ in range(1, max_steps):
        result.append((result[-1] * multiplier).quantize(Decimal("1"), rounding=ROUND_DOWN))
    return tuple(result)


DEFAULT_STRATEGY_CONFIG = StrategyConfig.from_payload({})
# Compatibility exports for existing imports. The engine reads saved config instead.
STAKE_STEPS = [int(value) for value in DEFAULT_STRATEGY_CONFIG.stakes]
BET_STEPS = STAKE_STEPS


class ScoreProgression(StrEnum):
    EXPECTED_GOAL = "EXPECTED_GOAL"
    UNCHANGED = "UNCHANGED"
    MISSED_EVENT = "MISSED_EVENT"
    INVALID = "INVALID"


def validate_score_progression(before: Score | Sequence[int], current: Score | Sequence[int], *, expected_goals: int) -> ScoreProgression:
    """Compare both score components and flag any additional/missed event."""
    old = before if isinstance(before, Score) else Score(*before)
    new = current if isinstance(current, Score) else Score(*current)
    delta_one, delta_two = new.team1 - old.team1, new.team2 - old.team2
    total_delta = delta_one + delta_two
    if delta_one < 0 or delta_two < 0:
        return ScoreProgression.INVALID
    if total_delta == expected_goals:
        return ScoreProgression.EXPECTED_GOAL
    if total_delta == 0:
        return ScoreProgression.UNCHANGED
    return ScoreProgression.MISSED_EVENT


def detect_scorer(previous_score: Score | Sequence[int], new_score: Score | Sequence[int]) -> Scorer:
    old = previous_score if isinstance(previous_score, Score) else Score(*previous_score)
    new = new_score if isinstance(new_score, Score) else Score(*new_score)
    delta1, delta2 = new.team1 - old.team1, new.team2 - old.team2
    if delta1 < 0 or delta2 < 0:
        return Scorer.UNKNOWN
    if delta1 + delta2 > 1:
        return Scorer.AMBIGUOUS_SCORE_CHANGE
    if delta1 > 0:
        return Scorer.TEAM_1
    if delta2 > 0:
        return Scorer.TEAM_2
    return Scorer.UNKNOWN


def can_create_initial_bet(score: Score, *, has_active_bet: bool = False) -> bool:
    return not has_active_bet and score.team1 == 0 and score.team2 == 0


def select_team_with_higher_odds(team1: str, team2: str, odds: NextGoalOdds) -> TeamSelection:
    if odds.team1 == odds.team2:
        raise ValueError("Equal odds do not select a team")
    if odds.team1 > odds.team2:
        return TeamSelection(team1, Scorer.TEAM_1, odds.team1, team2, odds.team2)
    return TeamSelection(team2, Scorer.TEAM_2, odds.team2, team1, odds.team1)


def odds_for_selected_side(odds: NextGoalOdds, side: Scorer) -> tuple[float, float]:
    if side == Scorer.TEAM_1:
        return odds.team1, odds.team2
    if side == Scorer.TEAM_2:
        return odds.team2, odds.team1
    raise ValueError(f"Invalid selected side: {side}")
