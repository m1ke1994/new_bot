from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.demo.engine import ENGINE
from backend.app.table_tennis.scanner import ScanAlreadyRunning, TableTennisScanError
from backend.app.table_tennis.sequential_forks_scanner import TABLE_TENNIS_SCANNER
from backend.app.table_tennis.state import TABLE_TENNIS_STATE


router = APIRouter(prefix="/api/table-tennis", tags=["table-tennis"])


class ForksStartRequest(BaseModel):
    budget: float = Field(gt=0)
    initial_stake: float = Field(gt=0)


def _ensure_football_worker_stopped() -> None:
    if ENGINE.task is not None and not ENGINE.task.done():
        raise HTTPException(
            status_code=409,
            detail="Остановите текущую футбольную стратегию перед сканированием настольного тенниса.",
        )


@router.post("/start")
async def start_table_tennis(payload: ForksStartRequest):
    _ensure_football_worker_stopped()
    try:
        return await TABLE_TENNIS_SCANNER.start_with_config(
            budget=payload.budget,
            initial_stake=payload.initial_stake,
        )
    except (ScanAlreadyRunning, ValueError) as error:
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


@router.get("/candidates")
async def table_tennis_candidates():
    return {"items": await TABLE_TENNIS_STATE.candidates()}


@router.get("/forks")
async def table_tennis_forks():
    return {"items": await TABLE_TENNIS_SCANNER.forks()}


@router.delete("/forks")
async def clear_table_tennis_forks():
    return await TABLE_TENNIS_SCANNER.clear_forks()
