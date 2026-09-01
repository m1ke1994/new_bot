import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .config import CONFIG
from .models import DemoStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_state() -> dict[str, Any]:
    return {
        "running": False,
        "browser": {"status": "CLOSED", "context": "CLOSED", "page": "CLOSED"},
        "auth": {"status": "UNKNOWN"},
        "mode": "DEMO",
        "status": DemoStatus.STOPPED.value,
        "league": CONFIG.league_name,
        "message": "Демо остановлено",
        "error": None,
        "event": None,
        "match": None,
        "scanner": {"total": 0, "started": 0, "upcoming": 0, "selected": None},
        "selected_team": None,
        "other_team": None,
        "selection_reason": None,
        "odds": {
            "selected": None,
            "opponent": None,
            "team1": None,
            "team2": None,
            "source": None,
            "confidence": None,
            "backend": None,
            "status": "WAITING",
        },
        "ocr": {
            "status": "WAITING",
            "attempt": 0,
            "max_attempts": CONFIG.ocr_max_attempts,
            "canvas": None,
            "engine": None,
            "latency_seconds": None,
            "candidates": [],
            "market_bbox": None,
        },
        "bet": {
            "step": 0,
            "max_steps": 11,
            "amount": None,
            "market": "Следующий гол",
            "odds": None,
            "score_before": None,
            "next_goal_number": None,
        },
        "last_change": None,
        "stats": {
            "matches_processed": 0,
            "bets": 0,
            "wins": 0,
            "losses": 0,
            "total_amount": 0,
            "average_odds": None,
            "max_step": 0,
            "average_steps_to_win": None,
        },
        "updated_at": utc_now(),
    }


class DemoStateStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = initial_state()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._state)

    async def reset_for_start(self, stats: dict[str, Any]) -> None:
        async with self._lock:
            self._state = initial_state()
            self._state.update(
                running=True,
                status=DemoStatus.OPENING_LEAGUE.value,
                message="Запуск DEMO worker",
                stats=deepcopy(stats),
                updated_at=utc_now(),
            )

    async def update(self, **changes: Any) -> dict[str, Any]:
        async with self._lock:
            self._state.update(deepcopy(changes))
            self._state["updated_at"] = utc_now()
            return deepcopy(self._state)


STATE = DemoStateStore()
