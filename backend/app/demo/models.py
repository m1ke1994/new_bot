from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class DemoStatus(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    AUTH_CHECK = "AUTH_CHECK"
    AUTHORIZED = "AUTHORIZED"
    OPENING_LEAGUE = "OPENING_LEAGUE"
    SCANNING_MATCHES = "SCANNING_UPCOMING_MATCHES"
    NO_UPCOMING_MATCHES = "NO_UPCOMING_MATCHES"
    MATCH_SELECTED = "MATCH_SELECTED"
    OPENING_MATCH = "OPENING_MATCH"
    WAITING_FOR_MATCH_START = "WAITING_FOR_MATCH_START"
    MATCH_STARTED = "MATCH_STARTED"
    OPENING_ADDITIONAL_MARKETS = "OPENING_ADDITIONAL_MARKETS"
    OPENING_GOALS_FILTER = "OPENING_GOALS_FILTER"
    WAITING_FOR_CANVAS = "WAITING_FOR_CANVAS"
    READING_ODDS = "READING_ODDS"
    WAITING_FOR_ODDS = "WAITING_FOR_ODDS"
    ODDS_READY = "ODDS_READY"
    TEAM_SELECTED = "TEAM_SELECTED"
    BET_SIMULATED = "BET_SIMULATED"
    WAITING_FOR_GOAL = "WAITING_FOR_NEXT_GOAL"
    GOAL_DETECTED = "GOAL_DETECTED"
    WIN = "WIN"
    LOSE = "LOSE"
    NEXT_STEP = "NEXT_STEP"
    RETURNING_TO_LEAGUE = "RETURNING_TO_LEAGUE"
    RECOVERING = "RECOVERING"
    SEQUENCE_EXHAUSTED = "SEQUENCE_EXHAUSTED"
    ERROR = "ERROR"


class Scorer(StrEnum):
    TEAM_1 = "TEAM_1"
    TEAM_2 = "TEAM_2"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS_SCORE_CHANGE = "AMBIGUOUS_SCORE_CHANGE"


@dataclass(frozen=True)
class Score:
    team1: int
    team2: int

    def text(self) -> str:
        return f"{self.team1}:{self.team2}"

    def as_list(self) -> list[int]:
        return [self.team1, self.team2]


@dataclass(frozen=True)
class ScoreboardSnapshot:
    team1: str
    team2: str
    score: Score
    timer: str
    period: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["score1"] = self.score.team1
        result["score2"] = self.score.team2
        result.pop("score")
        return result


@dataclass(frozen=True)
class NextGoalOdds:
    team1: float
    team2: float
    market: str = "Следующий гол"
    next_goal_number: int | None = None
    source: str = "CANVAS_OCR"
    ocr_backend: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class TeamSelection:
    selected_team: str
    selected_side: Scorer
    selected_odds: float
    other_team: str
    other_odds: float
