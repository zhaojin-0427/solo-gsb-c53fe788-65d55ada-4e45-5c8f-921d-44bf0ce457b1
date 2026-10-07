"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api.routes import router
from .db import db

log = logging.getLogger("contract-workbench")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    if db.available:
        log.info("PostgreSQL connected")
    else:
        log.warning("PostgreSQL unavailable (%s); using in-memory store",
                    db.last_error)
    yield


app = FastAPI(
    title="OpenAPI 契约演进工作台",
    description="导入基线/候选 OpenAPI 3.x 文档包，按服务端升级规则判定兼容性。",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static",
          StaticFiles(directory=str(STATIC_DIR)),
          name="static")
