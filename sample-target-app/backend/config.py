"""
All bottleneck knobs live here, driven by environment variables, so you can
launch the exact same code with different constraints to produce different,
known failure modes (needed for Phase 6 validation later).

Example:
    DB_POOL_SIZE=3 CPU_WORK_MS=200 uvicorn app:app --port 8000
"""
import os


class Config:
    # CPU bottleneck
    CPU_WORK_MS = int(os.getenv("CPU_WORK_MS", "20"))

    # Worker pool bottleneck (direct queue-depth measurement)
    WORKER_POOL_SIZE = int(os.getenv("WORKER_POOL_SIZE", "20"))

    # DB pool bottleneck
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
    DB_QUERY_MS = int(os.getenv("DB_QUERY_MS", "30"))

    # Downstream rate-limit bottleneck
    EXTERNAL_RATE_LIMIT_PER_SEC = int(os.getenv("EXTERNAL_RATE_LIMIT_PER_SEC", "1000"))
    EXTERNAL_LATENCY_MS = int(os.getenv("EXTERNAL_LATENCY_MS", "40"))

    # Memory bottleneck
    MEMORY_CHUNK_MB = int(os.getenv("MEMORY_CHUNK_MB", "5"))
    MEMORY_HOLD_MS = int(os.getenv("MEMORY_HOLD_MS", "500"))

    # Flaky endpoint
    FLAKY_FAILURE_RATE = float(os.getenv("FLAKY_FAILURE_RATE", "0.1"))
    FLAKY_MAX_DELAY_MS = int(os.getenv("FLAKY_MAX_DELAY_MS", "300"))


config = Config()