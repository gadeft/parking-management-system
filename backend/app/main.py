import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import Base, engine
from app import models  # noqa: F401 — імпорт потрібен для реєстрації моделей у metadata


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="Parking Management System API", lifespan=lifespan)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "instance": os.getenv("INSTANCE_NAME", "unknown"),
    }