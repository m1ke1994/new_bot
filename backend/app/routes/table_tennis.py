from fastapi import APIRouter, HTTPException

from backend.app.demo.engine import ENGINE
from backend.app.table_tennis.scanner import (
    ScanAlreadyRunning,
    TABLE_TENNIS_SCANNER,
    TableTennisScanError,
)
from backend.app.table_tennis.state import TABLE_TENNIS_STATE


router = APIRouter(prefix="/api/table-tennis", tags=["table-tennis"])


def _ensure_football_worker_stopped() -> None:
    if ENGINE.task is not None and not ENGINE.task.done():
        raise HTTPException(
            status_code=409,
            detail="Остановите текущую футбольную стратегию перед сканированием настольного тенниса.",
        )


@router.post("/start")
async def start_table_tennis():
    _ensure_football_worker_stopped()
    try:
        return await TABLE_TENNIS_SCANNER.start()
    except ScanAlreadyRunning as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/stop")
async def stop_table_tennis():
    return await TABLE_TENNIS_SCANNER.stop()


@router.post("/scan")
async def scan_table_tennis():
    _ensure_football_worker_stopped()
    try:
        return await TABLE_TENNIS_SCANNER.scan()
    except ScanAlreadyRunning as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TableTennisScanError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("/state")
async def table_tennis_state():
    state = await TABLE_TENNIS_STATE.snapshot()
    state["browser"] = await TABLE_TENNIS_SCANNER.browser_manager.snapshot()
    return state


@router.get("/leagues")
async def table_tennis_leagues():
    return {"items": await TABLE_TENNIS_STATE.leagues()}


@router.get("/matches")
async def table_tennis_matches():
    return {"items": await TABLE_TENNIS_STATE.matches()}
