from fastapi import APIRouter, HTTPException, Query

from backend.app.demo.engine import BrowserStartError, ENGINE, ModeConflictError
from backend.app.demo.history import REPOSITORY
from backend.app.demo.state import STATE
from backend.app.session_control import stop_and_reset_session


router = APIRouter(prefix="/api/live", tags=["live"])


@router.post("/start")
async def start_live():
    try:
        return await ENGINE.start("LIVE")
    except BrowserStartError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": error.code, "message": str(error)},
        ) from error
    except ModeConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/stop")
async def stop_live():
    try:
        return await stop_and_reset_session("LIVE")
    except ModeConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/state")
async def live_state():
    return await STATE.snapshot()


@router.get("/history")
async def live_history(limit: int = Query(default=500, ge=1, le=5000)):
    return {"items": await REPOSITORY.history(limit, mode="LIVE")}
