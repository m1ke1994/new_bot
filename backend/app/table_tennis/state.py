import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_table_tennis_state() -> dict[str, Any]:
    return {
        "status": "IDLE",
        "authorized": False,
        "auth_status": "UNKNOWN",
        "browser": {"status": "CLOSED", "context": "CLOSED", "page": "CLOSED"},
        "current_url": None,
        "scanning": False,
        "leagues_found": 0,
        "matches_found": 0,
        "candidates_count": 0,
        "active_match_id": None,
        "active_match": None,
        "current_league": None,
        "last_scan_started_at": None,
        "last_scan_finished_at": None,
        "updated_at": utc_now(),
        "error": None,
        "league_errors": [],
    }


class TableTennisStateStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = initial_table_tennis_state()
        self._leagues: list[dict[str, Any]] = []
        self._matches: list[dict[str, Any]] = []
        self._candidates: list[dict[str, Any]] = []

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._state)

    async def update(self, **changes: Any) -> dict[str, Any]:
        async with self._lock:
            self._state.update(deepcopy(changes), updated_at=utc_now())
            return deepcopy(self._state)

    async def replace_results(
        self,
        leagues: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self._lock:
            self._leagues = deepcopy(leagues)
            self._matches = deepcopy(matches)
            if candidates is not None:
                self._candidates = deepcopy(candidates)
                self._state.update(
                    candidates_count=len(candidates),
                    updated_at=utc_now(),
                )

    async def update_candidate(self, candidate: dict[str, Any]) -> None:
        async with self._lock:
            event_id = candidate.get("event_id")
            for index, current in enumerate(self._candidates):
                if event_id and current.get("event_id") == event_id:
                    self._candidates[index] = deepcopy(candidate)
                    break
            else:
                self._candidates.append(deepcopy(candidate))
            self._state.update(
                candidates_count=len(self._candidates),
                updated_at=utc_now(),
            )

    async def set_active_match(self, candidate: dict[str, Any] | None) -> None:
        async with self._lock:
            self._state.update(
                active_match_id=candidate.get("event_id") if candidate else None,
                active_match=deepcopy(candidate),
                updated_at=utc_now(),
            )

    async def leagues(self) -> list[dict[str, Any]]:
        async with self._lock:
            return deepcopy(self._leagues)

    async def matches(self) -> list[dict[str, Any]]:
        async with self._lock:
            return deepcopy(self._matches)

    async def candidates(self) -> list[dict[str, Any]]:
        async with self._lock:
            return deepcopy(self._candidates)


TABLE_TENNIS_STATE = TableTennisStateStore()
