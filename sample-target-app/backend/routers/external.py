"""
Downstream-rate-limit-bound endpoint. Simulates calling a third-party API
with its own rate limit — returns 429 once EXTERNAL_RATE_LIMIT_PER_SEC is
exceeded within the current 1-second window. This is the bottleneck type
that looks totally different from CPU/DB/memory: your OWN system's
resources look fine, but error rate climbs.
"""
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import config
from state import rate_limiter, inc

router = APIRouter(prefix="/api/external", tags=["external"])


class TaskResult(BaseModel):
    status: str
    external_ms: float


@router.post("/call", response_model=TaskResult)
async def call_downstream():
    import asyncio
    if not rate_limiter.allow():
        inc("rate_limited_rejections")
        raise HTTPException(status_code=429, detail="downstream rate limit exceeded")
    start = time.perf_counter()
    await asyncio.sleep(config.EXTERNAL_LATENCY_MS / 1000)
    inc("requests_served")
    return TaskResult(status="completed", external_ms=round((time.perf_counter() - start) * 1000, 1))