from __future__ import annotations

from enum import StrEnum

from backend.app.demo.models import Score, ScoreboardSnapshot


STRATEGY_NAME = "Ничья — 1-й тайм"
MARKET_NAME = "1X2. 1-й тайм"
MARKET_SELECTION = "Ничья"
PERIOD_KEY = "FIRST_HALF"
FIRST_HALF_END_TIMER = "03:00"


class FirstHalfPhase(StrEnum):
    UNKNOWN = "UNKNOWN"
    FIRST_HALF = "FIRST_HALF"
    FINISHED = "FIRST_HALF_FINISHED"


def _normalise(value: str) -> str:
    return " ".join(
        value.casefold()
        .replace("ё", "е")
        .replace("–", "-")
        .replace("—", "-")
        .split()
    )


def classify_first_half_phase(*signals: str) -> FirstHalfPhase:
    """Classify explicit site state; score itself is intentionally ignored."""
    status = " | ".join(_normalise(signal) for signal in signals if signal)
    finished_markers = (
        "перерыв",
        "half time",
        "halftime",
        "1-й тайм заверш",
        "1-й тайм окончен",
        "первый тайм заверш",
        "first half finished",
        "2-й тайм",
        "2 тайм",
        "второй тайм",
        "second half",
        "2nd half",
        "матч заверш",
        "матч окончен",
        "finished",
        "full time",
        "final",
    )
    if any(marker in status for marker in finished_markers):
        return FirstHalfPhase.FINISHED

    first_half_markers = (
        "1-й тайм",
        "1 тайм",
        "первый тайм",
        "first half",
        "1st half",
    )
    if any(marker in status for marker in first_half_markers):
        return FirstHalfPhase.FIRST_HALF
    return FirstHalfPhase.UNKNOWN


def is_first_half_end_timer(period: str, timer: str) -> bool:
    """Site-specific terminal flag: first half reaches exactly 03:00."""
    return (
        timer.strip() == FIRST_HALF_END_TIMER
        and classify_first_half_phase(period) == FirstHalfPhase.FIRST_HALF
    )


def is_first_half_finished(snapshot: ScoreboardSnapshot, *signals: str) -> bool:
    # In this FIFA 3x3 feed ``1-й тайм, 03:00`` is the end-of-half flag,
    # not a running timer. At that moment the scoreboard score is final for H1.
    if is_first_half_end_timer(snapshot.period, snapshot.timer):
        return True
    return (
        classify_first_half_phase(snapshot.period, snapshot.timer, *signals)
        == FirstHalfPhase.FINISHED
    )


def settle_first_half_draw(final_score: Score) -> str:
    return "WIN" if final_score.team1 == final_score.team2 else "LOSE"
