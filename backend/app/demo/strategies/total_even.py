from __future__ import annotations

from backend.app.demo.models import Score, ScoreboardSnapshot


STRATEGY_NAME = "Тотал чёт"
MARKET_NAME = "Тотал чёт"
MARKET_SELECTION = "Да"
MARKET_SEARCH_TEXT = "тотал чет"


def total_goals(score: Score) -> int:
    return score.team1 + score.team2


def total_parity(score: Score) -> str:
    return "EVEN" if total_goals(score) % 2 == 0 else "ODD"


def settle_total_even(final_score: Score) -> str:
    """Settle «Тотал чёт — Да» from an explicitly final scoreboard."""
    return "WIN" if total_parity(final_score) == "EVEN" else "LOSE"


def is_match_finished(snapshot: ScoreboardSnapshot) -> bool:
    """Require an explicit terminal marker; a score alone is never final."""
    status = " ".join(f"{snapshot.period} {snapshot.timer}".casefold().split())
    terminal_markers = (
        "заверш",
        "окончен",
        "матч окончен",
        "finished",
        "full time",
        "final",
    )
    return any(marker in status for marker in terminal_markers)
