"""
Flaky endpoint — randomly slow and randomly failing, independent of load
level. This is NOT a load-related bottleneck; it's a control case to make
sure your diagnosis agent can tell "this is just inherently unreliable"
apart from "this degrades under concurrency." A good diagnosis agent
should recognize flakiness doesn't correlate with concurrency level.
"""
import asyncio
import random
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import config
from state import inc

router = APIRouter(prefix="/api/flaky", tags=["flaky"])


class TaskResult(BaseModel):
    status: str
    delay_ms: float


@router.post("/call", response_model=TaskResult)
async def flaky_call():
    delay_ms = random.uniform(0, config.FLAKY_MAX_DELAY_MS)
    await asyncio.sleep(delay_ms / 1000)
    if random.random() < config.FLAKY_FAILURE_RATE:
        inc("flaky_failures")
        raise HTTPException(status_code=500, detail="random simulated failure")
    inc("requests_served")
    return TaskResult(status="completed", delay_ms=round(delay_ms, 1))