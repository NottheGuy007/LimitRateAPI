"""Admin endpoints: health check + live limiter stats."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/admin/health")
async def health():
    return {"status": "ok"}


@router.get("/admin/stats")
async def stats(request: Request):
    """Per-provider rate shaper snapshot.

    Useful for answering: "how deep is the queue right now?", "what cadence is
    configured?", "when did the last send happen?".
    """
    limiters = request.app.state.limiters
    rows = []
    for s in limiters.all_stats():
        rows.append(
            {
                "provider": s.provider,
                "mode": s.mode.value,
                "interval_seconds": round(s.interval_seconds, 4),
                "capacity": s.capacity,
                # tokens < 0 means N requests are queued ahead (debt); > 0 is
                # available burst budget.
                "tokens": round(s.tokens, 3),
                "queued_ahead": max(0, -int(s.tokens)) if s.tokens < 0 else 0,
                "waiting_in_acquire": s.waiting,
                "last_send_time": s.last_send_time,
                "max_concurrent": s.max_concurrent,
                "in_flight": s.in_flight,
            }
        )
    return JSONResponse({"providers": rows})
