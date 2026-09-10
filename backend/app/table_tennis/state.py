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
    ) -> None:
        async with self._lock:
            self._leagues = deepcopy(leagues)
            self._matches = deepcopy(matches)

    async def leagues(self) -> list[dict[str, Any]]:
        async with self._lock:
            return deepcopy(self._leagues)

    async def matches(self) -> list[dict[str, Any]]:
        async with self._lock:
            return deepcopy(self._matches)


TABLE_TENNIS_STATE = TableTennisStateStore()
