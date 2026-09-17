from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import backend.app.demo.engine as demo_engine_module
from backend.app.browser.canvas_locking_adapter import (
    read_next_goal_odds as visual_lock_read_next_goal_odds,
)
from backend.app.browser.manager import BROWSER_MANAGER

# Keep the current hybrid DOM+Canvas reader untouched and wrap only the
# next-goal call with an additional visual padlock check.
demo_engine_module.read_next_goal_odds = visual_lock_read_next_goal_odds
ENGINE = demo_engine_module.ENGINE

from backend.app.routes.browser import router as browser_router
from backend.app.routes.demo import router as demo_router
from backend.app.routes.live import router as live_router
from xbet_config import URL_CONFIG


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await ENGINE.restore()
    try:
        yield
    finally:
        await ENGINE.stop()
        await BROWSER_MANAGER.stop()


app = FastAPI(
    title="AutoBet DEMO API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(URL_CONFIG.backend_cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
app.include_router(demo_router)
app.include_router(browser_router)
app.include_router(live_router)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "autobet-demo"}


if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host=URL_CONFIG.backend_host,
        port=URL_CONFIG.backend_port,
        reload=False,
    )
