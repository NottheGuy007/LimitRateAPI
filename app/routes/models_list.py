"""GET /v1/models — aggregate model list across all providers.

Returns an OpenAI-compatible ``{object: "list", data: [...]}`` payload built
from the gateway's own registry, not the upstreams'. This is intentional: the
gateway's model namespace is what clients should code against, and querying
every upstream on every list call would be slow and hit rate limits.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    state = request.app.state.gateway
    now = int(time.time())
    data = [
        {
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": state.model_index.get(m, "unknown"),
        }
        for m in state.all_models()
    ]
    return {"object": "list", "data": data}
