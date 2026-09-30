import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable


LONG_SERIES_MIN_STEP = 4


class LongSeriesState(StrEnum):
    OFF = "OFF"
    WAITING_FOR_LONG = "WAITING_FOR_LONG"
    NEXT_MATCH_ALLOWED = "NEXT_MATCH_ALLOWED"
    LIVE_MATCH_ACTIVE = "LIVE_MATCH_ACTIVE"


class LongSeriesDecision(StrEnum):
    BYPASS = "BYPASS"
    SHADOW = "SHADOW"
    ALLOW = "ALLOW"
    DUPLICATE = "DUPLICATE"


PersistCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def default_long_series_runtime(*, enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "state": (
            LongSeriesState.WAITING_FOR_LONG.value
            if enabled
            else LongSeriesState.OFF.value
        ),
        "last_observed_series_length": None,
        "last_observed_series_match": None,
        "last_observed_series_match_id": None,
        "last_observed_is_long": None,
        "next_match_after_long_allowed": False,
        "permission_consumed": False,
        "active_match_id": None,
        "active_match": None,
        "shadow_match_id": None,
        "shadow_match": None,
        "shadow_series_step": 0,
        "next_match": "SKIP" if enabled else "ALLOWED",
        "active_observation": None,
        "observations": [],
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LongSeriesGate:
    """Atomic, match-level gate independent from the betting sequence state."""

    def __init__(self, persist: PersistCallback | None = None) -> None:
        self._lock = asyncio.Lock()
        self._persist = persist
        self._mode = "DEMO"
        self._runtime = default_long_series_runtime()

    async def load(
        self,
        *,
        enabled: bool,
        mode: str,
        persisted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._mode = mode.strip().upper()
            if not enabled:
                self._runtime = default_long_series_runtime(enabled=False)
            else:
                restored = {**default_long_series_runtime(enabled=True), **(persisted or {})}
                restored["enabled"] = True
                if restored.get("state") in {
                    LongSeriesState.OFF.value,
                    LongSeriesState.LIVE_MATCH_ACTIVE.value,
                }:
                    # An interrupted permission was already consumed. Never grant
                    # another match merely because the process restarted.
                    restored["state"] = LongSeriesState.WAITING_FOR_LONG.value
                    restored["next_match_after_long_allowed"] = False
                    restored["permission_consumed"] = False
                    restored["active_match_id"] = None
                    restored["active_match"] = None
                if restored["state"] == LongSeriesState.WAITING_FOR_LONG.value:
                    # An unfinished shadow observation cannot be resumed without
                    # reconstructing missed goals, so restart conservatively.
                    restored["shadow_match_id"] = None
                    restored["shadow_match"] = None
                    restored["shadow_series_step"] = 0
                    restored["active_observation"] = None
                restored["next_match"] = (
                    "ALLOWED"
                    if restored["state"] == LongSeriesState.NEXT_MATCH_ALLOWED.value
                    else "SKIP"
                )
                self._runtime = restored
            await self._save_locked()
            return deepcopy(self._runtime)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._runtime)

    async def claim_match(self, match_id: str, match_name: str) -> LongSeriesDecision:
        """Decide before the match series starts and consume at most one allow."""
        stable_id = str(match_id or "").strip()
        if not stable_id:
            raise ValueError("LONG_SERIES requires a stable match_id")
        async with self._lock:
            if not self._runtime["enabled"]:
                return LongSeriesDecision.BYPASS
            if stable_id == self._runtime.get("last_observed_series_match_id"):
                return LongSeriesDecision.DUPLICATE
            if self._runtime["state"] == LongSeriesState.NEXT_MATCH_ALLOWED.value:
                self._runtime.update(
                    state=LongSeriesState.LIVE_MATCH_ACTIVE.value,
                    next_match_after_long_allowed=False,
                    permission_consumed=True,
                    active_match_id=stable_id,
                    active_match=match_name,
                    shadow_match_id=None,
                    shadow_match=None,
                    shadow_series_step=0,
                    next_match="SKIP",
                    active_observation=None,
                )
                await self._save_locked()
                return LongSeriesDecision.ALLOW
            if self._runtime["state"] == LongSeriesState.LIVE_MATCH_ACTIVE.value:
                return LongSeriesDecision.DUPLICATE
            self._runtime.update(
                state=LongSeriesState.WAITING_FOR_LONG.value,
                shadow_match_id=stable_id,
                shadow_match=match_name,
                shadow_series_step=0,
                next_match="SKIP",
                active_observation={
                    "match_id": stable_id,
                    "match_name": match_name,
                    "selected_team": None,
                    "selected_side": None,
                    "side_label": None,
                    "initial_odds": None,
                    "current_odds": None,
                    "shadow_step": 0,
                    "score_before": None,
                    "score_after": None,
                    "scorer": None,
                    "step_result": "WAITING",
                    "series_length": 0,
                    "status": "OBSERVING",
                    "classification": "OBSERVING",
                    "started_at": utc_now(),
                    "completed_at": None,
                    "steps": [],
                },
            )
            await self._save_locked()
            return LongSeriesDecision.SHADOW

    async def start_observation(
        self,
        *,
        match_id: str,
        match_name: str,
        selected_team: str,
        selected_side: str,
        initial_odds: float,
    ) -> dict[str, Any]:
        async with self._lock:
            if str(match_id) != self._runtime.get("shadow_match_id"):
                raise RuntimeError("LONG_SERIES_SHADOW_MATCH_CHANGED")
            observation = self._runtime.get("active_observation") or {}
            observation.update(
                match_id=str(match_id),
                match_name=match_name,
                selected_team=selected_team,
                selected_side=selected_side,
                side_label=(
                    "Team 1" if selected_side == "TEAM_1" else "Team 2"
                ),
                initial_odds=float(initial_odds),
                current_odds=float(initial_odds),
                shadow_step=0,
                score_before=None,
                score_after=None,
                scorer=None,
                step_result="WAITING",
                series_length=0,
                status="OBSERVING",
                classification="OBSERVING",
                started_at=observation.get("started_at") or utc_now(),
                completed_at=None,
                steps=[],
            )
            self._runtime["active_observation"] = observation
            await self._save_locked()
            return deepcopy(self._runtime)

    async def begin_shadow_step(
        self,
        *,
        match_id: str,
        step: int,
        current_odds: float,
        score_before: str,
    ) -> dict[str, Any]:
        async with self._lock:
            observation = self._active_observation_locked(match_id)
            observation.update(
                current_odds=float(current_odds),
                shadow_step=int(step),
                score_before=score_before,
                score_after=None,
                scorer=None,
                step_result="WAITING",
                series_length=int(step),
                status="OBSERVING",
                classification="OBSERVING",
            )
            self._runtime["shadow_series_step"] = int(step)
            await self._save_locked()
            return deepcopy(self._runtime)

    async def record_shadow_step(
        self,
        *,
        match_id: str,
        match_name: str,
        step: int,
        result: str,
        current_odds: float | None = None,
        score_before: str | None = None,
        score_after: str | None = None,
        scorer: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if not self._runtime["enabled"]:
                return deepcopy(self._runtime)
            if str(match_id) != self._runtime.get("shadow_match_id"):
                raise RuntimeError("LONG_SERIES_SHADOW_MATCH_CHANGED")
            self._runtime["shadow_series_step"] = int(step)
            observation = self._runtime.get("active_observation")
            if observation is None:
                observation = {
                    "match_id": str(match_id),
                    "match_name": match_name,
                    "selected_team": None,
                    "selected_side": None,
                    "side_label": None,
                    "initial_odds": None,
                    "current_odds": current_odds,
                    "started_at": utc_now(),
                    "steps": [],
                }
                self._runtime["active_observation"] = observation
            step_record = {
                "step": int(step),
                "odds": current_odds,
                "score_before": score_before,
                "score_after": score_after,
                "scorer": scorer,
                "result": result.upper(),
            }
            steps = list(observation.get("steps") or [])
            if steps and int(steps[-1].get("step") or 0) == int(step):
                steps[-1] = step_record
            else:
                steps.append(step_record)
            observation.update(
                current_odds=current_odds,
                shadow_step=int(step),
                score_before=score_before,
                score_after=score_after,
                scorer=scorer,
                step_result=result.upper(),
                series_length=int(step),
                steps=steps,
            )
            if result.upper() == "WIN":
                is_long = int(step) >= LONG_SERIES_MIN_STEP
                classification = "LONG" if is_long else "SHORT"
                observation.update(
                    status=classification,
                    classification=classification,
                    completed_at=utc_now(),
                )
                self._append_observation_locked(observation)
                self._runtime.update(
                    state=(
                        LongSeriesState.NEXT_MATCH_ALLOWED.value
                        if is_long
                        else LongSeriesState.WAITING_FOR_LONG.value
                    ),
                    last_observed_series_length=int(step),
                    last_observed_series_match=match_name,
                    last_observed_series_match_id=str(match_id),
                    last_observed_is_long=is_long,
                    next_match_after_long_allowed=is_long,
                    permission_consumed=False,
                    shadow_match_id=None,
                    shadow_match=None,
                    next_match="ALLOWED" if is_long else "SKIP",
                    active_observation=None,
                )
            await self._save_locked()
            return deepcopy(self._runtime)

    async def finish_allowed_match(self, match_id: str) -> dict[str, Any]:
        async with self._lock:
            if (
                self._runtime["enabled"]
                and self._runtime["state"] == LongSeriesState.LIVE_MATCH_ACTIVE.value
                and str(match_id) == self._runtime.get("active_match_id")
            ):
                self._runtime.update(
                    state=LongSeriesState.WAITING_FOR_LONG.value,
                    next_match_after_long_allowed=False,
                    permission_consumed=False,
                    active_match_id=None,
                    active_match=None,
                    next_match="SKIP",
                )
                await self._save_locked()
            return deepcopy(self._runtime)

    async def finish_shadow_without_win(
        self, *, match_id: str, match_name: str, steps: int
    ) -> dict[str, Any]:
        async with self._lock:
            if str(match_id) == self._runtime.get("shadow_match_id"):
                observation = self._runtime.get("active_observation")
                if observation is not None:
                    observation.update(
                        shadow_step=int(steps),
                        series_length=int(steps),
                        status="INTERRUPTED",
                        classification="INTERRUPTED",
                        completed_at=utc_now(),
                    )
                    self._append_observation_locked(observation)
                self._runtime.update(
                    state=LongSeriesState.WAITING_FOR_LONG.value,
                    last_observed_series_length=int(steps),
                    last_observed_series_match=match_name,
                    last_observed_series_match_id=str(match_id),
                    last_observed_is_long=None,
                    next_match_after_long_allowed=False,
                    shadow_match_id=None,
                    shadow_match=None,
                    next_match="SKIP",
                    active_observation=None,
                )
                await self._save_locked()
            return deepcopy(self._runtime)

    def _active_observation_locked(self, match_id: str) -> dict[str, Any]:
        observation = self._runtime.get("active_observation")
        if observation is None or str(observation.get("match_id")) != str(match_id):
            raise RuntimeError("LONG_SERIES_OBSERVATION_NOT_ACTIVE")
        return observation

    def _append_observation_locked(self, observation: dict[str, Any]) -> None:
        observations = list(self._runtime.get("observations") or [])
        observations.append(deepcopy(observation))
        self._runtime["observations"] = observations[-20:]

    async def _save_locked(self) -> None:
        if self._persist is not None:
            await self._persist(self._mode, deepcopy(self._runtime))
