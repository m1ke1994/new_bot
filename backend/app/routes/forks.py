"""Isolated virtual and verified bookmaker-training-account fork endpoints."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal

from backend.app.virtual_forks import FORKS

router = APIRouter(prefix="/api/forks", tags=["virtual-forks"])


class StartPayload(BaseModel):
    budget: float = Field(ge=20, le=1_000_000_000)
    allocation_percent: float = Field(default=10, ge=1, le=100)
    placement_mode: Literal["VIRTUAL", "BOOKMAKER_DEMO"] = "VIRTUAL"
    confirm_demo: bool = False
    no_goal_risk_enabled: bool = False


@router.get("/state")
async def state():
    return FORKS.snapshot()


@router.post("/start")
async def start(payload: StartPayload):
    try:
        return await FORKS.start(
            payload.budget, payload.allocation_percent,
            placement_mode=payload.placement_mode,
            confirm_demo=payload.confirm_demo,
            no_goal_risk_enabled=payload.no_goal_risk_enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/stop")
async def stop():
    return await FORKS.stop()


@router.get("/history")
async def history(limit: int = Query(200, ge=1, le=5000)):
    return {"items": FORKS.store.history(limit)}


@router.get("/logs")
async def logs(limit: int = Query(200, ge=1, le=1000)):
    return {"items": FORKS.store.logs(limit)}


@router.get("/observations")
async def observations(limit: int = Query(300, ge=1, le=2000)):
    return {"items": FORKS.store.observations(limit)}
