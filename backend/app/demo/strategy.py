from collections.abc import Sequence

from .models import NextGoalOdds, Score, Scorer, TeamSelection


STAKE_STEPS = [25, 56, 126, 284, 639, 1438, 3348]

# Backwards-compatible name used by the existing worker and API tests.
BET_STEPS = STAKE_STEPS


def detect_scorer(
    previous_score: Score | Sequence[int],
    new_score: Score | Sequence[int],
) -> Scorer:
    old = previous_score if isinstance(previous_score, Score) else Score(*previous_score)
    new = new_score if isinstance(new_score, Score) else Score(*new_score)

    delta1 = new.team1 - old.team1
    delta2 = new.team2 - old.team2

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
    """The first bet is allowed once, only while the scoreboard is exactly 0:0."""
    return not has_active_bet and score.team1 == 0 and score.team2 == 0


def select_team_with_higher_odds(
    team1: str,
    team2: str,
    odds: NextGoalOdds,
) -> TeamSelection:
    if odds.team1 == odds.team2:
        raise ValueError("Коэффициенты равны: команда по стратегии не определяется.")

    if odds.team1 > odds.team2:
        return TeamSelection(
            selected_team=team1,
            selected_side=Scorer.TEAM_1,
            selected_odds=odds.team1,
            other_team=team2,
            other_odds=odds.team2,
        )

    return TeamSelection(
        selected_team=team2,
        selected_side=Scorer.TEAM_2,
        selected_odds=odds.team2,
        other_team=team1,
        other_odds=odds.team1,
    )


def odds_for_selected_side(odds: NextGoalOdds, side: Scorer) -> tuple[float, float]:
    if side == Scorer.TEAM_1:
        return odds.team1, odds.team2
    if side == Scorer.TEAM_2:
        return odds.team2, odds.team1
    raise ValueError(f"Некорректная сторона выбранной команды: {side}")
