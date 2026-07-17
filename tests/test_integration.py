"""End-to-end integration tests with a mock upstream.

Uses FastAPI's TestClient (httpx-based) against the real app, with the
upstream calls mocked via respx. This verifies the full stack:
config → registry → limiter → route → proxy → upstream.
"""
from __future__ import annotations

import asyncio
import time

import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    AppConfig,
    ProviderConfig,
    RateLimitConfig,
    RateLimitMode,
    ServerConfig,
)


def _make_app(
    spaced_rpm: float = 60,
    max_wait=None,
    retry_upstream_429: bool = True,
    max_upstream_retries: int = 5,
    upstream_retry_backoff_seconds: float = 1.0,
) -> TestClient:
    """Build an app instance with one 'mock' provider at the given rate."""
    cfg = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers=[
            ProviderConfig(
                name="mock",
                base_url="https://mock.upstream/v1",
                api_key="sk-mock",
                models=["gpt-test"],
                rate_limit=RateLimitConfig(
                    mode=RateLimitMode.SPACED,
                    rpm=spaced_rpm,
                    max_wait_seconds=max_wait,
                    retry_upstream_429=retry_upstream_429,
                    max_upstream_retries=max_upstream_retries,
                    upstream_retry_backoff_seconds=upstream_retry_backoff_seconds,
                ),
            )
        ],
    )
    # Inject the config directly so TestClient doesn't load from disk.
    return TestClient(create_app(config=cfg))


@respx.mock
def test_chat_completion_proxies_to_upstream():
    """A single request flows through to the upstream and returns its body."""
    client = _make_app(spaced_rpm=600)  # very high rate => no waiting
    respx.post("https://mock.upstream/v1/chat/completions").mock(
        return_value=respx.MockResponse(
            status_code=200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            },
        )
    )
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": [{"role": "user", "content": "yo"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "hi"


@respx.mock
def test_model_not_found_returns_404():
    client = _make_app()
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "no-such-model", "messages": []},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "model_not_found"


@respx.mock
def test_missing_model_field_returns_400():
    client = _make_app()
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["type"] == "invalid_request_error"


@respx.mock
def test_rate_limiting_delays_back_to_back_requests():
    """Two requests to a 1-req/2s provider: first instant, second waits ~2s."""
    client = _make_app(spaced_rpm=30)  # 60/30 = 2s interval
    respx.post("https://mock.upstream/v1/chat/completions").mock(
        return_value=respx.MockResponse(
            status_code=200,
            json={"id": "x", "choices": []},
        )
    )
    with client:
        t0 = time.monotonic()
        r1 = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": []},
        )
        r2 = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": []},
        )
        elapsed = time.monotonic() - t0
    assert r1.status_code == 200 and r2.status_code == 200
    # Second request had to wait ~2s for the next slot.
    assert elapsed >= 1.8, f"rate limit not enforced: {elapsed:.2f}s < 1.8s"


@respx.mock
def test_max_wait_returns_503():
    """With max_wait=0.1 and a 2s interval, the second request times out."""
    client = _make_app(spaced_rpm=30, max_wait=0.1)
    respx.post("https://mock.upstream/v1/chat/completions").mock(
        return_value=respx.MockResponse(status_code=200, json={"id": "x", "choices": []})
    )
    with client:
        r1 = client.post(
            "/v1/chat/completions", json={"model": "gpt-test", "messages": []}
        )
        r2 = client.post(
            "/v1/chat/completions", json={"model": "gpt-test", "messages": []}
        )
    assert r1.status_code == 200
    assert r2.status_code == 503
    err = r2.json()["detail"]["error"]
    assert err["type"] == "rate_limit_queue_timeout"
    assert err["retry_after"] > 0
    assert r2.headers.get("retry-after") is not None


@respx.mock
def test_sse_streaming_is_forwarded():
    """stream=true requests get SSE chunks forwarded as they arrive."""
    client = _make_app(spaced_rpm=600)
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: [DONE]\n\n'
    )
    respx.post("https://mock.upstream/v1/chat/completions").mock(
        return_value=respx.MockResponse(
            status_code=200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )
    )
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": [], "stream": True},
        )
    assert resp.status_code == 200
    text = resp.text
    assert "hel" in text and "lo" in text and "[DONE]" in text


@respx.mock
def test_upstream_429_is_retried_transparently():
    """Upstream returns 429 once (with a short Retry-After), then 200. The
    client should only ever see the eventual 200 -- the 429 is absorbed."""
    client = _make_app(spaced_rpm=6000, upstream_retry_backoff_seconds=0.05)
    route = respx.post("https://mock.upstream/v1/chat/completions")
    route.side_effect = [
        respx.MockResponse(
            status_code=429,
            json={"status": 429, "title": "Too Many Requests"},
            headers={"retry-after": "0.05"},
        ),
        respx.MockResponse(
            status_code=200, json={"id": "x", "choices": [{"message": {"content": "ok"}}]}
        ),
    ]
    with client:
        t0 = time.monotonic()
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": []},
        )
        elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    assert route.call_count == 2
    # Should have honored the ~0.05s Retry-After before the second attempt.
    assert elapsed >= 0.04


@respx.mock
def test_upstream_429_exhausts_retries_and_forwards_429():
    """If the upstream keeps returning 429 past max_upstream_retries, the
    client eventually does see a 429 -- retries aren't infinite."""
    client = _make_app(
        spaced_rpm=6000,
        max_upstream_retries=2,
        upstream_retry_backoff_seconds=0.02,
    )
    route = respx.post("https://mock.upstream/v1/chat/completions")
    route.mock(
        return_value=respx.MockResponse(
            status_code=429, json={"status": 429, "title": "Too Many Requests"}
        )
    )
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": []},
        )
    assert resp.status_code == 429
    # Initial attempt + 2 retries = 3 total calls.
    assert route.call_count == 3


@respx.mock
def test_upstream_429_retry_disabled_forwards_immediately():
    """retry_upstream_429=False preserves the old pure-passthrough behavior:
    the first 429 goes straight to the client, no retry attempted."""
    client = _make_app(spaced_rpm=6000, retry_upstream_429=False)
    route = respx.post("https://mock.upstream/v1/chat/completions")
    route.mock(
        return_value=respx.MockResponse(
            status_code=429, json={"status": 429, "title": "Too Many Requests"}
        )
    )
    with client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-test", "messages": []},
        )
    assert resp.status_code == 429
    assert route.call_count == 1


def test_models_list():
    client = _make_app(spaced_rpm=600)
    with client:
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    assert "gpt-test" in ids


def test_admin_stats_reports_provider_state():
    client = _make_app(spaced_rpm=60)
    with client:
        resp = client.get("/admin/stats")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert len(providers) == 1
    p = providers[0]
    assert p["provider"] == "mock"
    assert p["mode"] == "spaced"
    assert abs(p["interval_seconds"] - 1.0) < 0.01


def test_health():
    client = _make_app()
    with client:
        resp = client.get("/admin/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
