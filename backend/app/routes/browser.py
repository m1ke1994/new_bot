from fastapi import APIRouter

from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.demo.engine import ENGINE


router = APIRouter(prefix="/api/browser", tags=["browser"])


@router.post("/start")
async def browser_start():
    await BROWSER_MANAGER.start()
    return await BROWSER_MANAGER.snapshot()


@router.post("/stop")
async def browser_stop():
    await ENGINE.stop()
    return await BROWSER_MANAGER.stop()


@router.get("/state")
async def browser_state():
    return await BROWSER_MANAGER.snapshot()


@router.post("/market-canvas-debug")
async def market_canvas_debug():
    return await ENGINE.canvas_debug()
