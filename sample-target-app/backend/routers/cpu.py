"""
CPU-bound endpoints — three versions, on purpose:

  /sync           - a plain `def` handler. FastAPI runs these in a thread
                     pool (default 40 threads). Real CPU work here still
                     contends for actual CPU cores, so under load this
                     saturates CPU AND eventually exhausts the thread
                     pool too — two bottlenecks tangled together, which
                     is realistic but slightly messier to diagnose.

  /async          - an `async def` handler that does the SAME CPU work
                     directly in the event loop. This is a common
                     real-world mistake: putting blocking CPU work in an
                     async function blocks the whole event loop, not
                     just one thread — usually WORSE under load than
                     the sync version.

  /worker-limited - CPU work behind an explicit, tunable worker pool
                     (WORKER_POOL_SIZE). This gives a DIRECT, honest
                     queue-depth measurement instead of making you infer
                     queueing indirectly from CPU% patterns.
"""
import asyncio
import time
from fastapi import APIRouter
from pydantic import BaseModel
from config import config
from state import inc, worker_pool

router = APIRouter(prefix="/api/cpu", tags=["cpu"])


class TaskResult(BaseModel):
    status: str
    cpu_ms: float


class WorkerLimitedResult(BaseModel):
    status: str
    cpu_ms: float
    queue_wait_ms: float


def burn_cpu(duration_ms: int) -> float:
    start = time.perf_counter()
    end = start + duration_ms / 1000
    x = 0
    while time.perf_counter() < end:
        x += 1
    return (time.perf_counter() - start) * 1000


@router.post("/sync", response_model=TaskResult)
def cpu_sync():
    """Blocking `def` — FastAPI thread-pool bound."""
    cpu_ms = burn_cpu(config.CPU_WORK_MS)
    inc("requests_served")
    return TaskResult(status="completed", cpu_ms=round(cpu_ms, 1))


@router.post("/async", response_model=TaskResult)
async def cpu_async():
    """`async def` doing blocking CPU work directly — blocks the event
    loop for every concurrent request. Expect this to degrade WORSE
    than /sync under high concurrency."""
    cpu_ms = burn_cpu(config.CPU_WORK_MS)
    inc("requests_served")
    return TaskResult(status="completed", cpu_ms=round(cpu_ms, 1))


@router.post("/worker-limited", response_model=WorkerLimitedResult)
async def cpu_worker_limited():
    """
    CPU work behind an explicit worker pool (size WORKER_POOL_SIZE).
    This is the endpoint built specifically to give a DIRECT queue-depth
    signal — no more inferring queueing from indirect CPU% patterns.

    "queue_wait_ms" tells you exactly how long THIS request sat waiting
    for a free worker before it started, and "queued_for_worker" /
    "active_workers" in /metrics tell you the live picture across all
    requests at once. This is the missing piece that should let the AI
    reach the specific "worker-pool exhaustion / queueing" diagnosis
    directly, instead of landing on "unclear resource contention."
    """
    inc("queued_for_worker")
    wait_start = time.perf_counter()
    async with worker_pool:
        queue_wait_ms = (time.perf_counter() - wait_start) * 1000
        inc("queued_for_worker", -1)
        inc("active_workers")
        try:
            # Run on a REAL separate thread — this is what lets multiple
            # requests' CPU work genuinely overlap in time, which is
            # necessary for the worker-pool semaphore to show real
            # queueing. If we called burn_cpu() directly here, it would
            # block the single event loop and force accidental
            # serialization regardless of the semaphore.
            cpu_ms = await asyncio.to_thread(burn_cpu, config.CPU_WORK_MS)
        finally:
            inc("active_workers", -1)
    inc("requests_served")
    return WorkerLimitedResult(
        status="completed",
        cpu_ms=round(cpu_ms, 1),
        queue_wait_ms=round(queue_wait_ms, 1),
    )