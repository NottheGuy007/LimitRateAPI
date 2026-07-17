"""Chat & text-completion proxy routes with rate shaping.

Shared flow for both endpoints:
    resolve provider → wait for a concurrency slot → acquire rate slot
    (may wait/queue) → proxy upstream.
Concurrency is gated before pacing so the send-rate token is only ever
spent immediately before the real send. (Pacing before concurrency would
let requests "pre-pay" their token while stuck behind max_concurrent, then
all get released in a burst once slots free up -- defeating the configured
interval right when it matters most, i.e. a slow/congested upstream.)
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..proxy import proxy_request
from ..rate_limiter import RateLimitTimeoutError
from ..registry import GatewayState, ProviderNotFound

router = APIRouter()


def _get_state(request: Request) -> GatewayState:
    return request.app.state.gateway


def _get_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def _resolve_provider(state: GatewayState, body: dict):
    """Resolve the provider from the request body's 'model' field.

    Returns (provider_config, error_json_response_if_invalid_body).
    """
    model = body.get("model")
    if not model or not isinstance(model, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "request body must include a string 'model' field",
                    "type": "invalid_request_error",
                }
            },
        )
    try:
        return state.resolve(model)
    except ProviderNotFound as e:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "message": str(e),
                    "type": "model_not_found",
                }
            },
        ) from e


async def _handle(
    request: Request, state: GatewayState, client: httpx.AsyncClient, path: str
):
    # Read body once and stash on request.state so the proxy layer can detect
    # stream=true and reuse the parsed JSON.
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"invalid JSON body: {e}",
                               "type": "invalid_request_error"}},
        ) from e
    request.state.json_body = body

    provider = _resolve_provider(state, body)

    # The heart of the gateway: wait for a rate slot before issuing the upstream
    # call. On timeout we surface a 503 with Retry-After.
    limiter = request.app.state.limiters.get(provider.name)
    concurrency_held = False
    try:
        if limiter is not None:
            # Concurrency gate FIRST, pacing gate SECOND. If pacing ran first,
            # a request could "spend" its send-rate token while still queued
            # behind max_concurrent (slow upstream). Once several slots free
            # up together, all those already-paced requests would be let
            # through the semaphore at once and fire to upstream within
            # milliseconds of each other -- a burst that blows straight
            # through the configured interval and trips the upstream's own
            # 429s. Pacing must be the last gate before the actual send.
            await limiter.acquire_concurrency_slot()
            concurrency_held = True
            await limiter.acquire()
    except RateLimitTimeoutError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": str(e),
                    "type": "rate_limit_queue_timeout",
                    "retry_after": round(e.retry_after, 3),
                }
            },
            headers={"Retry-After": str(int(e.retry_after) + 1)},
        ) from e

    # Once we hold the slot(s), proxy upstream. Errors here are forwarded
    # as-is (we don't pretend the gateway failed when the upstream did).
    try:
        response = await proxy_request(provider, request, path, client, limiter)
    except Exception:
        if concurrency_held:
            limiter.release_concurrency_slot()
        raise

    if concurrency_held:
        # Wrap the streamed body so the concurrency slot isn't freed until
        # the response is fully sent (or the client disconnects and the
        # upstream connection is closed) -- NOT merely once headers arrive.
        upstream_iterator = response.body_iterator

        async def _iterate_then_release():
            try:
                async for chunk in upstream_iterator:
                    yield chunk
            finally:
                limiter.release_concurrency_slot()

        response.body_iterator = _iterate_then_release()

    return response


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions. Streams SSE transparently when
    ``stream: true``."""
    state = _get_state(request)
    client = _get_client(request)
    # Upstream path is the same suffix; providers' base_url already includes
    # the /v1 prefix (e.g. https://api.openai.com/v1).
    return await _handle(request, state, client, "/chat/completions")


@router.post("/v1/completions")
async def completions(request: Request):
    """OpenAI-compatible legacy text completions."""
    state = _get_state(request)
    client = _get_client(request)
    return await _handle(request, state, client, "/completions")
