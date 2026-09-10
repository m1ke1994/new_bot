from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TableTennisLeague:
    league_id: str
    name: str
    href: str
    url: str
    declared_games_count: int | None = None
    parsed_games_count: int = 0
    group_name: str | None = None

    @property
    def games_count(self) -> int | None:
        return self.declared_games_count

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["games_count"] = self.games_count
        return result


@dataclass(frozen=True)
class TableTennisMatch:
    event_id: str | None
    league_id: str
    league_name: str
    player_1: str
    player_2: str
    score: str | None
    sets_score: str | None
    current_set: int | None
    points_player_1: int | None
    points_player_2: int | None
    status: str
    started: bool | None
    time: str | None
    href: str | None
    url: str | None
    odds: dict[str, float] = field(default_factory=dict)
    markets: dict[str, Any] = field(default_factory=dict)
    raw_score_values: list[str] = field(default_factory=list)
    period: str | None = None
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
