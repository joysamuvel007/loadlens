"""
Baseline endpoint — does essentially nothing. This is your calibration
control: run your load generator against this FIRST at high concurrency.
If latency climbs here too, the bottleneck is in your load generator or
network stack, not the target — a critical sanity check before you trust
any diagnosis from the other endpoints.
"""
from fastapi import APIRouter
from state import inc, counters

router = APIRouter(prefix="/api/baseline", tags=["baseline"])


@router.post("/quick")
async def quick():
    inc("requests_served")
    return {"status": "ok"}