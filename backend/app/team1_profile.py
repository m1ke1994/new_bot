import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable


DEFAULT_TEAM1_PROFILE_MAX_ODDS = Decimal("2.00")
PersistCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_team1_profile_allowed(
    selected_side: str,
    selected_odds: Decimal | float | int | str,
    *,
    enabled: bool,
    max_odds: Decimal | float | int | str = DEFAULT_TEAM1_PROFILE_MAX_ODDS,
) -> bool:
    """Allow only the selected left side (TEAM_1) below the exclusive max odds."""
    if not enabled:
        return True
    if str(selected_side).strip().upper() != "TEAM_1":
        return False
    try:
        return Decimal(str(selected_odds)) < Decimal(str(max_odds))
    except (InvalidOperation, TypeError, ValueError):
        return False


def rejection_reason(
    selected_side: str,
    selected_odds: Decimal | float | int | str,
    *,
    max_odds: Decimal | float | int | str,
) -> str:
    side_ok = str(selected_side).strip().upper() == "TEAM_1"
    try:
        odds_ok = Decimal(str(selected_odds)) < Decimal(str(max_odds))
    except (InvalidOperation, TypeError, ValueError):
        odds_ok = False
    if not side_ok and not odds_ok:
        return "NOT_TEAM_1_AND_ODDS_NOT_BELOW_MAX"
    if not side_ok:
        return "SELECTED_TEAM_IS_TEAM_2"
    return "ODDS_NOT_BELOW_MAX"


def default_team1_profile_runtime(
    *,
    enabled: bool = False,
    max_odds: Decimal | float | int | str = DEFAULT_TEAM1_PROFILE_MAX_ODDS,
) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "max_odds": float(Decimal(str(max_odds))),
        "state": "READY" if enabled else "OFF",
        "active_observation": None,
        "observations": [],
    }


class Team1ProfileRuntime:
    """Persistent shadow history for matches rejected by the TEAM_1 odds profile."""

    def __init__(self, persist: PersistCallback | None = None) -> None:
        self._lock = asyncio.Lock()
        self._persist = persist
        self._mode = "DEMO"
        self._runtime = default_team1_profile_runtime()

    async def load(
        self,
        *,
        enabled: bool,
        max_odds: Decimal | float | int | str,
        mode: str,
        persisted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._mode = mode.strip().upper()
            restored = {
                **default_team1_profile_runtime(enabled=enabled, max_odds=max_odds),
                **(persisted or {}),
            }
            restored["enabled"] = bool(enabled)
            restored["max_odds"] = float(Decimal(str(max_odds)))

            active = restored.get("active_observation")
            if active:
                interrupted = deepcopy(active)
                interrupted.update(
                    status="INTERRUPTED",
                    completion_status="INTERRUPTED",
                    completed_at=interrupted.get("completed_at") or utc_now(),
                )
                observations = list(restored.get("observations") or [])
                observations.append(interrupted)
                restored["observations"] = observations[-50:]
                restored["active_observation"] = None

            restored["state"] = "READY" if enabled else "OFF"
            self._runtime = restored
            await self._save_locked()
            return deepcopy(self._runtime)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._runtime)

    async def start_observation(
        self,
        *,
        match_id: str,
        match_name: str,
        selected_team: str,
        selected_side: str,
        initial_odds: float,
        reason: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._runtime["state"] = "OBSERVING"
            self._runtime["active_observation"] = {
                "match_id": str(match_id),
                "match_name": match_name,
                "selected_team": selected_team,
                "selected_side": selected_side,
                "side_label": "Команда 1" if selected_side == "TEAM_1" else "Команда 2",
                "initial_odds": float(initial_odds),
                "current_odds": float(initial_odds),
                "filter_reason": reason,
                "shadow_step": 0,
                "series_length": 0,
                "score_before": None,
                "score_after": None,
                "scorer": None,
                "step_result": "WAITING",
                "status": "OBSERVING",
                "completion_status": "ACTIVE",
                "started_at": utc_now(),
                "completed_at": None,
                "steps": [],
            }
            await self._save_locked()
            return deepcopy(self._runtime)

    async def begin_step(
        self,
        *,
        match_id: str,
        step: int,
        current_odds: float,
        score_before: str,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._active_locked(match_id)
            observation.update(
                current_odds=float(current_odds),
                shadow_step=int(step),
                series_length=int(step),
                score_before=score_before,
                score_after=None,
                scorer=None,
                step_result="WAITING",
            )
            await self._save_locked()
            return deepcopy(self._runtime)

    async def record_step(
        self,
        *,
        match_id: str,
        step: int,
        result: str,
        current_odds: float | None = None,
        score_before: str | None = None,
        score_after: str | None = None,
        scorer: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._active_locked(match_id)
            normalized_result = str(result).upper()
            record = {
                "step": int(step),
                "odds": current_odds,
                "score_before": score_before,
                "score_after": score_after,
                "scorer": scorer,
                "result": normalized_result,
            }
            steps = list(observation.get("steps") or [])
            if steps and int(steps[-1].get("step") or 0) == int(step):
                steps[-1] = record
            else:
                steps.append(record)
            observation.update(
                current_odds=current_odds,
                shadow_step=int(step),
                series_length=int(step),
                score_before=score_before,
                score_after=score_after,
                scorer=scorer,
                step_result=normalized_result,
                steps=steps,
            )
            if normalized_result == "WIN":
                observation.update(
                    status="COMPLETED",
                    completion_status="COMPLETED",
                    completed_at=utc_now(),
                )
                self._append_locked(observation)
                self._runtime["active_observation"] = None
                self._runtime["state"] = "READY"
            await self._save_locked()
            return deepcopy(self._runtime)

    async def finish_without_win(
        self,
        *,
        match_id: str,
        steps: int,
        reason: str,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._runtime.get("active_observation")
            if observation is not None and str(observation.get("match_id")) == str(match_id):
                observation.update(
                    shadow_step=int(steps),
                    series_length=int(steps),
                    status="INTERRUPTED",
                    completion_status="INTERRUPTED",
                    interruption_reason=reason,
                    completed_at=utc_now(),
                )
                self._append_locked(observation)
                self._runtime["active_observation"] = None
            self._runtime["state"] = "READY" if self._runtime.get("enabled") else "OFF"
            await self._save_locked()
            return deepcopy(self._runtime)

    def _active_locked(self, match_id: str) -> dict[str, Any]:
        observation = self._runtime.get("active_observation")
        if observation is None or str(observation.get("match_id")) != str(match_id):
            raise RuntimeError("TEAM1_PROFILE_OBSERVATION_NOT_ACTIVE")
        return observation

    def _append_locked(self, observation: dict[str, Any]) -> None:
        observations = list(self._runtime.get("observations") or [])
        observations.append(deepcopy(observation))
        self._runtime["observations"] = observations[-50:]

    async def _save_locked(self) -> None:
        if self._persist is not None:
            await self._persist(self._mode, deepcopy(self._runtime))
