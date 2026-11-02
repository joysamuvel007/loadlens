"""
Mixed endpoint — CPU work, then a DB-pool checkout, then a downstream
call, all in one request. This is the realistic "everything at once"
case: use this for your main end-to-end ramp tests once you've already
validated each bottleneck type individually via the isolated endpoints
in cpu.py / db.py / external.py / memory.py.
"""
import asyncio
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import config
from state import db_pool, rate_limiter, inc

router = APIRouter(prefix="/api/mixed", tags=["mixed"])


class TaskResult(BaseModel):
    status: str
    cpu_ms: float
    db_wait_ms: float
    external_ms: float
    total_ms: float


def burn_cpu(duration_ms: int) -> float:
    start = time.perf_counter()
    end = start + duration_ms / 1000
    x = 0
    while time.perf_counter() < end:
        x += 1
    return (time.perf_counter() - start) * 1000


@router.post("/process", response_model=TaskResult)
async def process():
    inc("in_flight_total")
    total_start = time.perf_counter()
    try:
        cpu_ms = burn_cpu(config.CPU_WORK_MS)

        wait_start = time.perf_counter()
        async with db_pool:
            db_wait_ms = (time.perf_counter() - wait_start) * 1000
            await asyncio.sleep(config.DB_QUERY_MS / 1000)

        if not rate_limiter.allow():
            inc("rate_limited_rejections")
            raise HTTPException(status_code=429, detail="downstream rate limit exceeded")
        ext_start = time.perf_counter()
        await asyncio.sleep(config.EXTERNAL_LATENCY_MS / 1000)
        external_ms = (time.perf_counter() - ext_start) * 1000

        inc("requests_served")
        return TaskResult(
            status="completed",
            cpu_ms=round(cpu_ms, 1),
            db_wait_ms=round(db_wait_ms, 1),
            external_ms=round(external_ms, 1),
            total_ms=round((time.perf_counter() - total_start) * 1000, 1),
        )
    finally:
        inc("in_flight_total", -1)