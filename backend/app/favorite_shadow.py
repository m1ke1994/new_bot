import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from backend.app.demo.models import NextGoalOdds, Scorer, TeamSelection


FAVORITE_SHADOW_MAX_STEPS = 9
FAVORITE_SHADOW_HISTORY_LIMIT = 200
PersistCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_favorite_with_lower_odds(
    team1: str,
    team2: str,
    odds: NextGoalOdds,
) -> TeamSelection | None:
    """Return the strict lower-odds side. Equal odds have no favorite."""
    if odds.team1 == odds.team2:
        return None
    if odds.team1 < odds.team2:
        return TeamSelection(team1, Scorer.TEAM_1, odds.team1, team2, odds.team2)
    return TeamSelection(team2, Scorer.TEAM_2, odds.team2, team1, odds.team1)


def favorite_team1_entry_selection(
    team1: str,
    team2: str,
    odds: NextGoalOdds,
    *,
    min_odds: float = 0.0,
) -> TeamSelection | None:
    """Allow only a strict TEAM_1 favorite at or above the configured minimum odds."""
    favorite = select_favorite_with_lower_odds(team1, team2, odds)
    if favorite is None or favorite.selected_side != Scorer.TEAM_1:
        return None
    if favorite.selected_odds < float(min_odds):
        return None
    return favorite


def favorite_team1_rejection_reason(
    team1: str,
    team2: str,
    odds: NextGoalOdds,
    *,
    min_odds: float = 0.0,
) -> str | None:
    favorite = select_favorite_with_lower_odds(team1, team2, odds)
    if favorite is None:
        return "EQUAL_INITIAL_ODDS"
    if favorite.selected_side != Scorer.TEAM_1:
        return "FAVORITE_IS_TEAM_2"
    if favorite.selected_odds < float(min_odds):
        return "FAVORITE_ODDS_BELOW_MIN"
    return None


def default_favorite_shadow_runtime(*, enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "max_steps": FAVORITE_SHADOW_MAX_STEPS,
        "state": "READY" if enabled else "OFF",
        "active_observations": {},
        "observations": [],
        "stats": {
            "completed": 0,
            "exhausted": 0,
            "interrupted": 0,
            "skipped": 0,
            "wins_by_step": {str(step): 0 for step in range(1, FAVORITE_SHADOW_MAX_STEPS + 1)},
        },
    }


class FavoriteShadowRuntime:
    """Persistent read-only favorite-to-score observations.

    Multiple observations may run at once because the main betting worker is
    allowed to leave a match immediately while FAVORITE SHADOW keeps watching
    that match in its own browser tab.
    """

    def __init__(self, persist: PersistCallback | None = None) -> None:
        self._lock = asyncio.Lock()
        self._persist = persist
        self._mode = "DEMO"
        self._runtime = default_favorite_shadow_runtime()

    async def load(
        self,
        *,
        enabled: bool,
        mode: str,
        persisted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._mode = mode.strip().upper()
            restored = {
                **default_favorite_shadow_runtime(enabled=enabled),
                **(persisted or {}),
            }
            restored["enabled"] = bool(enabled)
            restored["max_steps"] = FAVORITE_SHADOW_MAX_STEPS

            # Browser tabs cannot survive a backend restart reliably. Keep the
            # old data, but finalize in-flight observations as interrupted
            # instead of pretending their result is known.
            active = dict(restored.get("active_observations") or {})
            observations = list(restored.get("observations") or [])
            if active:
                for observation in active.values():
                    interrupted = deepcopy(observation)
                    interrupted.update(
                        status="INTERRUPTED",
                        completion_status="INTERRUPTED",
                        interruption_reason="BACKEND_RESTART",
                        completed_at=interrupted.get("completed_at") or utc_now(),
                    )
                    observations.append(interrupted)
                active = {}

            restored["active_observations"] = active
            restored["observations"] = observations[-FAVORITE_SHADOW_HISTORY_LIMIT:]
            restored["stats"] = self._rebuild_stats(restored["observations"])
            restored["state"] = "READY" if enabled else "OFF"
            self._runtime = restored
            await self._save_locked()
            return deepcopy(self._runtime)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._runtime)

    async def has_match(self, match_id: str) -> bool:
        expected = str(match_id)
        async with self._lock:
            if expected in (self._runtime.get("active_observations") or {}):
                return True
            return any(
                str(item.get("match_id") or "") == expected
                for item in (self._runtime.get("observations") or [])
            )

    async def record_skipped(
        self,
        *,
        match_id: str,
        match_name: str,
        reason: str,
        team1: str | None = None,
        team2: str | None = None,
        score: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            key = str(match_id)
            if key in (self._runtime.get("active_observations") or {}) or any(
                str(item.get("match_id") or "") == key
                for item in (self._runtime.get("observations") or [])
            ):
                return deepcopy(self._runtime)
            observation = {
                "match_id": key,
                "match_name": match_name,
                "team1": team1,
                "team2": team2,
                "favorite_team": None,
                "favorite_side": None,
                "favorite_side_label": None,
                "outsider_team": None,
                "initial_favorite_odds": None,
                "initial_outsider_odds": None,
                "shadow_step": 0,
                "score_before": score,
                "score_after": score,
                "current_favorite_odds": None,
                "market_ready": False,
                "status": "SKIPPED",
                "completion_status": "SKIPPED",
                "interruption_reason": str(reason),
                "winning_step": None,
                "started_at": utc_now(),
                "completed_at": utc_now(),
                "steps": [],
            }
            observations = list(self._runtime.get("observations") or [])
            observations.append(observation)
            observations = observations[-FAVORITE_SHADOW_HISTORY_LIMIT:]
            self._runtime["observations"] = observations
            self._runtime["stats"] = self._rebuild_stats(observations)
            self._runtime["state"] = (
                "OBSERVING"
                if self._runtime.get("active_observations")
                else ("READY" if self._runtime.get("enabled") else "OFF")
            )
            await self._save_locked()
            return deepcopy(self._runtime)

    async def start_observation(
        self,
        *,
        match_id: str,
        match_name: str,
        team1: str,
        team2: str,
        favorite_team: str,
        favorite_side: str,
        initial_favorite_odds: float,
        outsider_team: str,
        initial_outsider_odds: float,
    ) -> dict[str, Any]:
        async with self._lock:
            key = str(match_id)
            active = dict(self._runtime.get("active_observations") or {})
            if key in active:
                return deepcopy(self._runtime)
            if any(
                str(item.get("match_id") or "") == key
                for item in (self._runtime.get("observations") or [])
            ):
                return deepcopy(self._runtime)

            active[key] = {
                "match_id": key,
                "match_name": match_name,
                "team1": team1,
                "team2": team2,
                "favorite_team": favorite_team,
                "favorite_side": favorite_side,
                "favorite_side_label": (
                    "Команда 1" if favorite_side == Scorer.TEAM_1.value else "Команда 2"
                ),
                "outsider_team": outsider_team,
                "initial_favorite_odds": float(initial_favorite_odds),
                "initial_outsider_odds": float(initial_outsider_odds),
                "shadow_step": 1,
                "score_before": "0:0",
                "score_after": None,
                "current_favorite_odds": float(initial_favorite_odds),
                "market_ready": True,
                "status": "OBSERVING",
                "completion_status": "ACTIVE",
                "winning_step": None,
                "started_at": utc_now(),
                "completed_at": None,
                "steps": [],
            }
            self._runtime["active_observations"] = active
            self._runtime["state"] = "OBSERVING"
            await self._save_locked()
            return deepcopy(self._runtime)

    async def begin_step(
        self,
        *,
        match_id: str,
        step: int,
        score_before: str,
        favorite_odds: float | None,
        market_ready: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._active_locked(match_id)
            observation.update(
                shadow_step=int(step),
                score_before=score_before,
                score_after=None,
                current_favorite_odds=favorite_odds,
                market_ready=bool(market_ready),
            )
            await self._save_locked()
            return deepcopy(self._runtime)

    async def record_goal(
        self,
        *,
        match_id: str,
        step: int,
        result: str,
        score_before: str,
        score_after: str,
        scorer: str,
        favorite_odds: float | None,
        market_ready: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._active_locked(match_id)
            normalized = str(result).upper()
            record = {
                "step": int(step),
                "favorite_odds": favorite_odds,
                "market_ready": bool(market_ready),
                "score_before": score_before,
                "score_after": score_after,
                "scorer": scorer,
                "result": normalized,
            }
            steps = list(observation.get("steps") or [])
            steps.append(record)
            observation.update(
                shadow_step=int(step),
                score_before=score_before,
                score_after=score_after,
                current_favorite_odds=favorite_odds,
                market_ready=bool(market_ready),
                steps=steps,
            )

            if normalized == "WIN":
                observation.update(
                    winning_step=int(step),
                    status="COMPLETED",
                    completion_status="COMPLETED",
                    completed_at=utc_now(),
                )
                self._complete_locked(str(match_id), observation)
            elif int(step) >= FAVORITE_SHADOW_MAX_STEPS:
                observation.update(
                    winning_step=None,
                    status="EXHAUSTED",
                    completion_status="EXHAUSTED",
                    completed_at=utc_now(),
                )
                self._complete_locked(str(match_id), observation)

            await self._save_locked()
            return deepcopy(self._runtime)

    async def interrupt(self, *, match_id: str, reason: str) -> dict[str, Any]:
        async with self._lock:
            key = str(match_id)
            active = dict(self._runtime.get("active_observations") or {})
            observation = active.get(key)
            if observation is None:
                return deepcopy(self._runtime)
            observation.update(
                status="INTERRUPTED",
                completion_status="INTERRUPTED",
                interruption_reason=str(reason),
                completed_at=utc_now(),
            )
            self._complete_locked(key, observation)
            await self._save_locked()
            return deepcopy(self._runtime)

    def _active_locked(self, match_id: str) -> dict[str, Any]:
        key = str(match_id)
        observation = (self._runtime.get("active_observations") or {}).get(key)
        if observation is None:
            raise RuntimeError("FAVORITE_SHADOW_OBSERVATION_NOT_ACTIVE")
        return observation

    def _complete_locked(self, key: str, observation: dict[str, Any]) -> None:
        active = dict(self._runtime.get("active_observations") or {})
        active.pop(key, None)
        observations = list(self._runtime.get("observations") or [])
        observations.append(deepcopy(observation))
        observations = observations[-FAVORITE_SHADOW_HISTORY_LIMIT:]
        self._runtime["active_observations"] = active
        self._runtime["observations"] = observations
        self._runtime["stats"] = self._rebuild_stats(observations)
        self._runtime["state"] = "OBSERVING" if active else (
            "READY" if self._runtime.get("enabled") else "OFF"
        )

    @staticmethod
    def _rebuild_stats(observations: list[dict[str, Any]]) -> dict[str, Any]:
        wins = {str(step): 0 for step in range(1, FAVORITE_SHADOW_MAX_STEPS + 1)}
        exhausted = 0
        interrupted = 0
        skipped = 0
        completed = 0
        for item in observations:
            status = str(item.get("completion_status") or "")
            if status == "COMPLETED":
                completed += 1
                winning_step = int(item.get("winning_step") or 0)
                if 1 <= winning_step <= FAVORITE_SHADOW_MAX_STEPS:
                    wins[str(winning_step)] += 1
            elif status == "EXHAUSTED":
                exhausted += 1
            elif status == "INTERRUPTED":
                interrupted += 1
            elif status == "SKIPPED":
                skipped += 1
        return {
            "completed": completed,
            "exhausted": exhausted,
            "interrupted": interrupted,
            "skipped": skipped,
            "wins_by_step": wins,
        }

    async def _save_locked(self) -> None:
        if self._persist is not None:
            await self._persist(self._mode, deepcopy(self._runtime))
