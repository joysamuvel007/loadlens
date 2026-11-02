"""
DB-pool-bound endpoint. Requests beyond DB_POOL_SIZE genuinely queue on
the semaphore in state.py, just like a real connection pool. Tighten
DB_POOL_SIZE via env var to force a clean, known DB-bound bottleneck.
"""
import asyncio
import time
from fastapi import APIRouter
from pydantic import BaseModel
from config import config
from state import db_pool, inc

router = APIRouter(prefix="/api/db", tags=["db"])


class TaskResult(BaseModel):
    status: str
    db_wait_ms: float
    db_query_ms: float


@router.post("/query", response_model=TaskResult)
async def db_query():
    inc("waiting_for_db_pool")
    wait_start = time.perf_counter()
    async with db_pool:
        wait_ms = (time.perf_counter() - wait_start) * 1000
        inc("waiting_for_db_pool", -1)
        inc("db_pool_in_use")
        query_start = time.perf_counter()
        await asyncio.sleep(config.DB_QUERY_MS / 1000)
        query_ms = (time.perf_counter() - query_start) * 1000
        inc("db_pool_in_use", -1)
    inc("requests_served")
    return TaskResult(status="completed", db_wait_ms=round(wait_ms, 1), db_query_ms=round(query_ms, 1))