from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.browser.manager import BROWSER_MANAGER
from backend.app.demo.engine import ENGINE
from backend.app.routes.browser import router as browser_router
from backend.app.routes.demo import router as demo_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(demo_router)
app.include_router(browser_router)


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "autobet-demo"}


if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=False)
