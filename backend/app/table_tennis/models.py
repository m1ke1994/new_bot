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
    is_candidate: bool = False
    odds: dict[str, float] = field(default_factory=dict)
    markets: dict[str, Any] = field(default_factory=dict)
    raw_score_values: list[str] = field(default_factory=list)
    period: str | None = None
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TableTennisObservation:
    event_id: str
    league_id: str
    league_name: str
    player_1: str
    player_2: str
    current_set: int
    target_set: int
    match_score: str | None
    set_score: str | None
    href: str | None
    url: str
    initial_odds_p1: float | None = None
    initial_odds_p2: float | None = None
    current_odds_p1: float | None = None
    current_odds_p2: float | None = None
    initial_odds_at: str | None = None
    odds_updated_at: str | None = None
    market_status: str = "WAITING_FOR_MARKET"
    monitoring_status: str = "MATCH_SELECTED"
    odds_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_match(
        cls,
        match: TableTennisMatch,
        *,
        current_set: int,
        target_set: int,
    ) -> "TableTennisObservation":
        if not match.event_id or not match.url:
            raise ValueError("Для мониторинга обязательны event_id и URL матча.")
        return cls(
            event_id=match.event_id,
            league_id=match.league_id,
            league_name=match.league_name,
            player_1=match.player_1,
            player_2=match.player_2,
            current_set=current_set,
            target_set=target_set,
            match_score=match.score,
            set_score=match.sets_score,
            href=match.href,
            url=match.url,
        )

    def record_odds(self, p1: float, p2: float, timestamp: str) -> dict[str, Any]:
        previous_p1 = self.current_odds_p1
        previous_p2 = self.current_odds_p2
        first = self.initial_odds_p1 is None or self.initial_odds_p2 is None
        restored = self.market_status == "MARKET_LOCKED"
        if first:
            self.initial_odds_p1 = p1
            self.initial_odds_p2 = p2
            self.initial_odds_at = timestamp
        changed = first or previous_p1 != p1 or previous_p2 != p2
        self.current_odds_p1 = p1
        self.current_odds_p2 = p2
        self.odds_updated_at = timestamp
        self.market_status = "MARKET_AVAILABLE"
        self.monitoring_status = "MONITORING"
        if changed:
            self.odds_history.append({"timestamp": timestamp, "p1": p1, "p2": p2})
        return {
            "first": first,
            "changed": changed,
            "restored": restored,
            "previous_p1": previous_p1,
            "previous_p2": previous_p2,
        }

    def mark_market_locked(self, timestamp: str) -> bool:
        changed = self.market_status != "MARKET_LOCKED"
        self.market_status = "MARKET_LOCKED"
        self.monitoring_status = "MONITORING"
        self.current_odds_p1 = None
        self.current_odds_p2 = None
        self.odds_updated_at = timestamp
        return changed

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["is_candidate"] = self.current_set in (1, 2)
        result["outcomes"] = {
            "p1": {"player": self.player_1, "odds": self.current_odds_p1},
            "p2": {"player": self.player_2, "odds": self.current_odds_p2},
        }
        return result
