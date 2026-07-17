"""Upstream proxying via httpx, with transparent SSE streaming."""
from __future__ import annotations

import asyncio
import json
import random
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Optional, TYPE_CHECKING

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from .models import ProviderConfig

if TYPE_CHECKING:  # pragma: no cover - import-cycle avoidance only
    from .rate_limiter import RateLimiter

# Headers that must NOT be forwarded upstream (hop-by-hop / gateway-owned).
# We re-set Authorization ourselves from the provider's api_key.
_HOP_BY_HOP = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
}
_OWNED = {"authorization"}


def _forward_headers(src: Request, api_key: str) -> dict[str, str]:
    """Build the upstream header dict from the inbound request.

    Drops hop-by-hop headers and the inbound Authorization (we replace it with
    the provider's own key).
    """
    out: dict[str, str] = {}
    for k, v in src.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk in _OWNED:
            continue
        out[k] = v
    out["Authorization"] = f"Bearer {api_key}"
    return out


def _is_stream(req: Request) -> bool:
    """Best-effort detection of stream=true chat/completion requests."""
    # Cheap path: inspect the parsed body if FastAPI already read it.
    try:
        body = req.state.json_body
    except AttributeError:
        return False
    return bool(body.get("stream"))


def _upstream_url(provider: ProviderConfig, path: str) -> str:
    base = provider.base_url.rstrip("/")
    return f"{base}{path}"


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header: either delta-seconds (e.g. "3") or an
    HTTP-date. Returns None if absent or unparseable (caller falls back to
    its own backoff in that case)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        import time as _time

        delta = dt.timestamp() - _time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _upstream_retry_backoff(provider: ProviderConfig, attempt: int) -> float:
    """Exponential backoff with jitter, used when the upstream 429 has no
    (or an unparseable) Retry-After header. attempt is 0-indexed."""
    base = provider.rate_limit.upstream_retry_backoff_seconds
    delay = min(base * (2 ** attempt), 30.0)
    return delay + random.uniform(0, delay * 0.25)


async def _read_json_body(req: Request) -> dict:
    """Read & cache the request body as JSON on req.state for reuse."""
    cached = getattr(req.state, "json_body", None)
    if cached is not None:
        return cached
    raw = await req.body()
    if not raw:
        body: dict = {}
    else:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"request body is not valid JSON: {e}") from e
    req.state.json_body = body
    return body


async def proxy_request(
    provider: ProviderConfig,
    req: Request,
    path: str,
    client: httpx.AsyncClient,
    limiter: Optional["RateLimiter"] = None,
) -> StreamingResponse:
    """Forward a request to one upstream provider.

    Always returns a StreamingResponse: for non-stream calls we stream the
    single upstream body through (cheap), for stream calls we forward SSE
    chunks as they arrive. This unifies the response path.

    If the upstream itself returns HTTP 429 and ``provider.rate_limit.
    retry_upstream_429`` is enabled, we transparently retry (honoring the
    upstream's Retry-After header, or backing off exponentially) instead of
    forwarding the 429 to the caller. This is safe to do even for SSE
    ("stream": true) requests: httpx's ``client.send(..., stream=True)``
    returns the response status/headers as soon as they arrive, BEFORE the
    body is read -- so we always know whether an attempt failed before a
    single byte has been sent to our own caller. Only once an attempt
    succeeds (or retries are exhausted) do we start streaming anything back.

    ``limiter``, if given, is re-consulted before each *retry* (not the
    first attempt, whose slot was already acquired by the caller) so a
    stream of retries stays paced through the same send-rate limiter rather
    than hammering the upstream a second, unpaced way.
    """
    body = await _read_json_body(req)
    headers = _forward_headers(req, provider.api_key)
    url = _upstream_url(provider, path)

    rl_cfg = provider.rate_limit
    attempt = 0
    while True:
        upstream_req = client.build_request(
            req.method, url, headers=headers, json=body if body else None
        )
        upstream_resp = await client.send(upstream_req, stream=True)

        # Retry on 429 (rate limit) or 5xx (server error) — both indicate
        # the upstream is temporarily unable to serve. 5xx is common when
        # NVIDIA is overloaded (returns 500 "Failed to generate completions").
        is_retryable = (
            upstream_resp.status_code == 429
            or upstream_resp.status_code >= 500
        )
        if (
            is_retryable
            and rl_cfg.retry_upstream_429
            and attempt < rl_cfg.max_upstream_retries
        ):
            wait = _parse_retry_after(upstream_resp.headers.get("retry-after"))
            if wait is None:
                wait = _upstream_retry_backoff(provider, attempt)
            await upstream_resp.aclose()
            attempt += 1
            await asyncio.sleep(wait)
            if limiter is not None:
                # Re-pace this retry through the same send-rate limiter so a
                # burst of retries doesn't itself become an unpaced storm.
                await limiter.acquire()
            continue

        break

    # Filter the upstream's response headers the same way (drop hop-by-hop).
    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    async def iterate() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        iterate(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def proxy_simple(
    provider: ProviderConfig,
    method: str,
    path: str,
    client: httpx.AsyncClient,
    json_body: Optional[dict] = None,
    api_key_override: Optional[str] = None,
) -> httpx.Response:
    """Non-streaming helper for simple GET/DELETE-style upstream calls
    (e.g. listing models). Returns the full response."""
    headers = {"Authorization": f"Bearer {api_key_override or provider.api_key}"}
    url = _upstream_url(provider, path)
    return await client.request(
        method, url, headers=headers, json=json_body, params=None
    )
