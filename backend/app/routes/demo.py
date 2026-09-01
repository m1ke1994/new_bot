from fastapi import APIRouter, Query

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


@router.get("/state")
async def demo_state():
    return await STATE.snapshot()


@router.get("/history")
async def demo_history(limit: int = Query(default=500, ge=1, le=5000)):
    return {"items": await REPOSITORY.history(limit)}


@router.get("/logs")
async def demo_logs(limit: int = Query(default=500, ge=1, le=5000)):
    return {"items": await REPOSITORY.logs(limit)}
