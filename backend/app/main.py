import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import models  # noqa: F401
from app.database import Base, engine
from app.routers import admin, auth, gate, parking, payments


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1001)"))
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Parking Management System API",
    description="Інформаційна система управління автоматизованим паркінгом",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(parking.router)
app.include_router(gate.router)
app.include_router(admin.router)
app.include_router(payments.router)


@app.get("/api/health", tags=["System"])
async def health_check():
    return {
        "status": "ok",
        "instance": os.getenv("INSTANCE_NAME", "unknown"),
    }