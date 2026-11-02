import asyncio
import time
import random
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="MedTranscribe-style load test target")

# In-memory counters so you can see concurrency pile-up during a test
active_requests = {"blocking": 0, "async": 0}


class TranscriptionJob(BaseModel):
    audio_id: str
    language: str = "ta"


class TranscriptionResult(BaseModel):
    audio_id: str
    status: str
    processing_time_ms: float
    active_requests_at_completion: int


def simulate_cpu_work(duration_s: float) -> None:
    """Busy-loop that genuinely occupies the CPU, like inference would."""
    end = time.time() + duration_s
    x = 0
    while time.time() < end:
        x += 1


@app.get("/health")
def health():
    return {"status": "ok", "active": active_requests}


@app.post("/transcribe/blocking", response_model=TranscriptionResult)
def transcribe_blocking(job: TranscriptionJob):
    """Plain `def`: runs in a finite thread pool and does CPU work on the request path."""
    active_requests["blocking"] += 1
    try:
        simulate_cpu_work(0.2)
        time.sleep(random.uniform(0.05, 0.15))   # blocking I/O simulation
        return TranscriptionResult(
            audio_id=job.audio_id, status="queued", processing_time_ms=200,
            active_requests_at_completion=active_requests["blocking"],
        )
    finally:
        active_requests["blocking"] -= 1


@app.post("/transcribe/async", response_model=TranscriptionResult)
async def transcribe_async(job: TranscriptionJob):
    """Async: the I/O wait yields to the event loop, but the CPU part still blocks it."""
    active_requests["async"] += 1
    try:
        simulate_cpu_work(0.2)
        await asyncio.sleep(random.uniform(0.05, 0.15))   # non-blocking I/O simulation
        return TranscriptionResult(
            audio_id=job.audio_id, status="queued", processing_time_ms=200,
            active_requests_at_completion=active_requests["async"],
        )
    finally:
        active_requests["async"] -= 1