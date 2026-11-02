"""
Main app — mounts every bottleneck-type router, exposes /health and
/metrics for live monitoring, and serves the frontend dashboard.

Run with:
    cd backend
    pip install -r requirements.txt --break-system-packages
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 to see the live dashboard, or point your
load generator/ramp_test.py at any endpoint under /api/*.
"""
import pathlib
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import config
from state import counters
from routers import baseline, cpu, db, memory, external, flaky, mixed

app = FastAPI(title="sample-target-app", description="Multi-bottleneck test target")

app.include_router(baseline.router)
app.include_router(cpu.router)
app.include_router(db.router)
app.include_router(memory.router)
app.include_router(external.router)
app.include_router(flaky.router)
app.include_router(mixed.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Polled by the frontend dashboard and by your Phase 2 metrics
    collector during a load test. Everything a diagnosis agent would
    need to distinguish bottleneck types lives here."""
    return {
        "config": {
            "CPU_WORK_MS": config.CPU_WORK_MS,
            "DB_POOL_SIZE": config.DB_POOL_SIZE,
            "DB_QUERY_MS": config.DB_QUERY_MS,
            "EXTERNAL_RATE_LIMIT_PER_SEC": config.EXTERNAL_RATE_LIMIT_PER_SEC,
            "EXTERNAL_LATENCY_MS": config.EXTERNAL_LATENCY_MS,
            "MEMORY_CHUNK_MB": config.MEMORY_CHUNK_MB,
        },
        "counters": counters,
    }


# Serve the simple dashboard frontend at "/"
frontend_dir = pathlib.Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")