from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.app.demo.engine import ENGINE
from backend.app.demo.history import REPOSITORY
from backend.app.demo.state import STATE


router = APIRouter(prefix="/api/demo", tags=["demo"])


@router.post("/start")
async def start_demo():
    return await ENGINE.start()


@router.post("/stop")
async def stop_demo():
    return await ENGINE.stop()


@router.post("/reset")
async def reset_demo_sequence():
    return await ENGINE.reset_sequence()


@router.get("/strategy-config")
async def get_strategy_config():
    return await REPOSITORY.get_config()


@router.put("/strategy-config")
async def save_strategy_config(payload: dict[str, Any]):
    try:
        return await ENGINE.save_strategy_config(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/budget")
async def demo_budget():
    return await REPOSITORY.get_budget()


@router.get("/state")
async def demo_state():
    return await STATE.snapshot()


@router.get("/history")
async def demo_history(limit: int = Query(default=500, ge=1, le=5000)):
    return {"items": await REPOSITORY.history(limit)}


@router.get("/logs")
async def demo_logs(limit: int = Query(default=500, ge=1, le=5000)):
    return {"items": await REPOSITORY.logs(limit)}
