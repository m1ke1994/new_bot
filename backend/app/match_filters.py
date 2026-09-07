from decimal import Decimal, InvalidOperation


MIN_INITIAL_SELECTED_ODDS = Decimal("1.93")
EXCLUDED_TEAMS = frozenset({"chelsea", "челси", "roma", "рома"})


def normalize_team_name(team: str | None) -> str:
    """Normalize a team label without using unsafe substring matching."""
    return " ".join((team or "").strip().casefold().split())


def excluded_team_in_match(team1: str | None, team2: str | None) -> str | None:
    for team in (team1, team2):
        if normalize_team_name(team) in EXCLUDED_TEAMS:
            return (team or "").strip()
    return None


def is_excluded_match(team1: str | None, team2: str | None) -> bool:
    return excluded_team_in_match(team1, team2) is not None


def is_initial_odds_allowed(selected_odds: Decimal | float | int | str) -> bool:
    """Apply the inclusive entry threshold using decimal arithmetic."""
    try:
        return Decimal(str(selected_odds)) >= MIN_INITIAL_SELECTED_ODDS
    except (InvalidOperation, TypeError, ValueError):
        return False
