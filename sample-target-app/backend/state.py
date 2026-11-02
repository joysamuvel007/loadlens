"""
Shared, process-wide state: the simulated DB connection pool, the
worker pool, the downstream rate limiter, and live counters. All routers
import from here so /metrics gives one consistent picture of what's
happening across every endpoint type at once — this is what your load
generator/metrics collector (Phase 2) will poll.
"""
import asyncio
import time
from dataclasses import dataclass, field

from config import config

# A real DB pool blocks a request when no connection is free. A semaphore
# reproduces that exactly: requests beyond DB_POOL_SIZE genuinely wait here.
db_pool = asyncio.Semaphore(config.DB_POOL_SIZE)

# Simulated worker pool for CPU-bound work — this directly measures how
# many requests are queued up WAITING for a free worker, versus how many
# are actively being processed. This is the direct queue-depth signal.
worker_pool = asyncio.Semaphore(config.WORKER_POOL_SIZE)


@dataclass
class RateLimiter:
    limit_per_sec: int
    window_start: float = field(default_factory=time.time)
    count_in_window: int = 0

    def allow(self) -> bool:
        now = time.time()
        if now - self.window_start >= 1.0:
            self.window_start = now
            self.count_in_window = 0
        self.count_in_window += 1
        return self.count_in_window <= self.limit_per_sec


rate_limiter = RateLimiter(config.EXTERNAL_RATE_LIMIT_PER_SEC)

# Live counters — polled by the frontend and available for your metrics
# collector to scrape during a load test.
counters = {
    "in_flight_total": 0,
    "waiting_for_db_pool": 0,
    "db_pool_in_use": 0,
    "rate_limited_rejections": 0,
    "flaky_failures": 0,
    "memory_chunks_held": 0,
    "requests_served": 0,
    "queued_for_worker": 0,
    "active_workers": 0,
}


def inc(key: str, by: int = 1):
    counters[key] = counters.get(key, 0) + by