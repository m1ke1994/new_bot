from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import backend.app.demo.engine as demo_engine_module
from backend.app.browser.canvas_locking_adapter import (
    read_next_goal_odds as visual_lock_read_next_goal_odds,
)
from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.demo.budget_sync import sync_strategy_budget
from backend.app.demo.fast_next_goal_runtime import install_fast_next_goal_runtime

# Use the single-frame Canvas reader with visual lock detection for NEXT_GOAL.
demo_engine_module.read_next_goal_odds = visual_lock_read_next_goal_odds
# DEMO virtual bets use the captured odds frame as the placement boundary.
# LIVE mode keeps the original confirmation and race-protection flow.
install_fast_next_goal_runtime(demo_engine_module)
ENGINE = demo_engine_module.ENGINE

from backend.app.routes.browser import router as browser_router
from backend.app.routes.demo import router as demo_router
from backend.app.routes.live import router as live_router
from xbet_config import URL_CONFIG


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Rebase any persisted legacy budget to the currently saved stake row before
    # the engine hydrates its in-memory DemoBudget instance.
    await sync_strategy_budget(update_state=False)
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
