"""
Memory-bound endpoint — allocates a real chunk of memory and holds it for
a duration before releasing. Under high concurrency this produces real,
observable memory growth (visible via psutil in Phase 2), distinct from
CPU or DB bottlenecks. Useful for teaching your diagnosis agent to tell
memory pressure apart from the others.
"""
import asyncio
from fastapi import APIRouter
from pydantic import BaseModel
from config import config
from state import inc

router = APIRouter(prefix="/api/memory", tags=["memory"])


class TaskResult(BaseModel):
    status: str
    chunk_mb: int
    held_ms: int


@router.post("/allocate", response_model=TaskResult)
async def allocate():
    inc("memory_chunks_held")
    # allocate a real chunk of memory (bytearray forces actual allocation,
    # unlike a lazy Python list)
    chunk = bytearray(config.MEMORY_CHUNK_MB * 1024 * 1024)
    try:
        await asyncio.sleep(config.MEMORY_HOLD_MS / 1000)
        # touch the memory so it isn't optimized away / stays resident
        chunk[0] = 1
    finally:
        inc("memory_chunks_held", -1)
        del chunk
    inc("requests_served")
    return TaskResult(status="completed", chunk_mb=config.MEMORY_CHUNK_MB, held_ms=config.MEMORY_HOLD_MS)