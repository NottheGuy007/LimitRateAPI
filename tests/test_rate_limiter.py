"""Tests for the rate limiter — the core of the project.

These verify:
- SPACED mode enforces a uniform interval between consecutive sends.
- BURST mode allows an initial burst, then smooths to RPM.
- Concurrent callers are released in FIFO order.
- max_wait_seconds causes a clean 503-bound timeout.
- A long idle period banks burst budget up to capacity, no more.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.models import RateLimitConfig, RateLimitMode
from app.rate_limiter import (
    LimiterRegistry,
    RateLimitTimeoutError,
    RateLimiter,
)


# ── SPACED mode ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spaced_first_call_is_immediate():
    """The very first request should not wait (bucket starts full)."""
    cfg = RateLimitConfig(mode=RateLimitMode.SPACED, rpm=60)  # 1/s
    lim = RateLimiter.from_config("p", cfg)
    t0 = time.monotonic()
    await lim.acquire()
    dt = time.monotonic() - t0
    assert dt < 0.05, f"first acquire should be instant, took {dt:.3f}s"


@pytest.mark.asyncio
async def test_spaced_enforces_uniform_interval():
    """60 RPM spaced => each send >= 1s after the previous, ~3s for 4 calls."""
    cfg = RateLimitConfig(mode=RateLimitMode.SPACED, rpm=60)  # interval = 1.0s
    lim = RateLimiter.from_config("p", cfg)
    starts: list[float] = []
    for i in range(4):
        await lim.acquire()
        starts.append(time.monotonic())
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    for g in gaps:
        # Allow a little scheduling slack below the target (we can't fire
        # FASTER than the interval), but never significantly so.
        assert g >= 0.95, f"gap {g:.3f}s < 0.95s — spaced mode leaked a token"
    total = starts[-1] - starts[0]
    assert 2.7 < total < 4.0, f"total {total:.3f}s outside expected ~3s window"


@pytest.mark.asyncio
async def test_spaced_concurrent_callers_release_in_fifo_order():
    """N concurrent acquires must be released in the order they arrived,
    each one interval apart."""
    cfg = RateLimitConfig(mode=RateLimitMode.SPACED, rpm=60)  # 1s interval
    lim = RateLimiter.from_config("p", cfg)
    order: list[int] = []

    async def worker(i: int):
        await lim.acquire()
        order.append(i)

    # Launch all at once with tiny stagger so arrival order is deterministic.
    tasks = []
    for i in range(5):
        tasks.append(asyncio.create_task(worker(i)))
        await asyncio.sleep(0.02)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3, 4], f"FIFO broken: got {order}"


@pytest.mark.asyncio
async def test_spaced_uses_stricter_of_rpm_and_explicit_interval():
    """rpm=60 (1s) and interval_seconds=2 => effective 2s."""
    cfg = RateLimitConfig(
        mode=RateLimitMode.SPACED, rpm=60, interval_seconds=2.0
    )
    assert cfg.effective_interval == 2.0
    lim = RateLimiter.from_config("p", cfg)
    t0 = time.monotonic()
    await lim.acquire()
    await lim.acquire()
    gap = time.monotonic() - t0
    assert gap >= 1.9, f"stricter interval not honored: {gap:.3f}s"


# ── BURST mode ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_burst_allows_initial_burst_then_smooths():
    """capacity=5, rpm=300 => first 5 fire near-instantly, the 6th waits ~0.2s."""
    cfg = RateLimitConfig(mode=RateLimitMode.BURST, rpm=300, capacity=5)
    # Refill rate = 300/60 = 5 tokens/s => 0.2s per token after burst.
    lim = RateLimiter.from_config("p", cfg)
    starts: list[float] = []
    for _ in range(7):
        await lim.acquire()
        starts.append(time.monotonic())
    # First 5 should be near-instant (within burst).
    burst_window = starts[4] - starts[0]
    assert burst_window < 0.2, f"burst not instant: {burst_window:.3f}s"
    # 6th must wait for at least one refill (>= ~0.15s).
    wait_for_6th = starts[5] - starts[4]
    assert wait_for_6th >= 0.15, f"6th fired too soon: {wait_for_6th:.3f}s"


@pytest.mark.asyncio
async def test_burst_idle_period_banks_only_up_to_capacity():
    """After an idle period, the bucket refills up to `capacity` and no more.

    Setup: rpm=60 (rate=1 token/s), capacity=3.
    - Drain the burst (3 immediate acquires → bucket ≈ 0).
    - Idle long enough to fully refill (3+ seconds → capped at capacity=3).
    - 3 more acquires should be instant; the 4th must wait ~1s for refill.
    """
    cfg = RateLimitConfig(mode=RateLimitMode.BURST, rpm=60, capacity=3)
    lim = RateLimiter.from_config("p", cfg)
    # Drain initial burst.
    for _ in range(3):
        await lim.acquire()
    # Idle to fully bank back to capacity (capacity/rate = 3/1 = 3s).
    await asyncio.sleep(3.3)
    starts: list[float] = []
    for _ in range(4):
        await lim.acquire()
        starts.append(time.monotonic())
    # First `capacity`=3 fire back-to-back instantly.
    burst = starts[2] - starts[0]
    assert burst < 0.2, f"didn't bank full burst capacity after idle: {burst:.3f}s"
    # 4th must wait for a refill (~1s with rate=1/s).
    wait_for_4th = starts[3] - starts[2]
    assert wait_for_4th >= 0.8, f"4th fired without refill wait: {wait_for_4th:.3f}s"


# ── max_wait_seconds ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_wait_timeout_raises():
    """When the queue is deeper than max_wait allows, acquire() raises
    RateLimitTimeoutError (which routes map to HTTP 503)."""
    cfg = RateLimitConfig(
        mode=RateLimitMode.SPACED, rpm=60, max_wait_seconds=0.1
    )
    lim = RateLimiter.from_config("p", cfg)
    await lim.acquire()  # consumes the initial token
    # Now the next caller would have to wait ~1s; max_wait=0.1 => timeout.
    with pytest.raises(RateLimitTimeoutError) as ei:
        await lim.acquire()
    assert ei.value.retry_after > 0


@pytest.mark.asyncio
async def test_max_wait_none_waits_forever():
    """max_wait_seconds=null (default) means wait until a slot is available."""
    cfg = RateLimitConfig(
        mode=RateLimitMode.SPACED, rpm=120, max_wait_seconds=None
    )  # 0.5s interval
    lim = RateLimiter.from_config("p", cfg)
    await lim.acquire()
    # This must eventually succeed rather than timing out.
    await asyncio.wait_for(lim.acquire(), timeout=2.0)


# ── max_concurrent (in-flight cap, independent of send-rate pacing) ───────────


@pytest.mark.asyncio
async def test_max_concurrent_none_is_uncapped():
    """No max_concurrent configured => concurrency slot calls are no-ops."""
    cfg = RateLimitConfig(mode=RateLimitMode.SPACED, rpm=6000)  # fast pacing
    lim = RateLimiter.from_config("p", cfg)
    # Should be able to "hold" many slots at once without blocking.
    for _ in range(20):
        await lim.acquire_concurrency_slot()
    assert lim.stats().in_flight == 0  # uncapped path never tracks in_flight


@pytest.mark.asyncio
async def test_max_concurrent_caps_in_flight_requests():
    """With max_concurrent=2, a 3rd concurrent slot must wait for one of the
    first two to release, even though send-rate pacing would allow it
    through immediately (rpm is high / non-limiting here)."""
    cfg = RateLimitConfig(
        mode=RateLimitMode.SPACED, rpm=6000, max_concurrent=2
    )
    lim = RateLimiter.from_config("p", cfg)

    released_third = False

    async def holder(release_after: float):
        await lim.acquire_concurrency_slot()
        try:
            await asyncio.sleep(release_after)
        finally:
            lim.release_concurrency_slot()

    async def third_waiter():
        nonlocal released_third
        await lim.acquire_concurrency_slot()
        released_third = True
        lim.release_concurrency_slot()

    t1 = asyncio.create_task(holder(0.3))
    t2 = asyncio.create_task(holder(0.3))
    await asyncio.sleep(0.05)  # let both slots be taken first
    assert lim.stats().in_flight == 2

    t3 = asyncio.create_task(third_waiter())
    await asyncio.sleep(0.05)
    # 3rd should still be blocked — both slots are held.
    assert released_third is False

    await asyncio.gather(t1, t2, t3)
    assert released_third is True
    assert lim.stats().in_flight == 0


@pytest.mark.asyncio
async def test_max_concurrent_independent_of_send_rate():
    """A high rpm (send-rate pacing wide open) does not bypass a tight
    max_concurrent cap — this is exactly the gap that let real in-flight
    requests pile up despite being within the configured rpm."""
    cfg = RateLimitConfig(mode=RateLimitMode.SPACED, rpm=6000, max_concurrent=1)
    lim = RateLimiter.from_config("p", cfg)

    order: list[str] = []

    async def worker(name: str):
        await lim.acquire()  # send-rate slot: effectively instant at rpm=6000
        await lim.acquire_concurrency_slot()
        order.append(f"{name}-start")
        await asyncio.sleep(0.1)
        order.append(f"{name}-end")
        lim.release_concurrency_slot()

    await asyncio.gather(worker("a"), worker("b"))
    # With max_concurrent=1, b cannot start until a has fully finished,
    # regardless of how permissive the send-rate pacing is.
    assert order == ["a-start", "a-end", "b-start", "b-end"]


@pytest.mark.asyncio
async def test_no_burst_when_concurrency_is_the_bottleneck():
    """Regression test: when max_concurrent is the real bottleneck (slow
    upstream), requests queued behind it must NOT burst through together
    once slots free up. This only holds if callers acquire the concurrency
    slot BEFORE the pacing token -- acquiring pacing first lets a request
    "pre-pay" its token while stuck on the semaphore, then several such
    requests get released in the same instant when slots free, blowing
    through the configured interval.
    """
    cfg = RateLimitConfig(
        mode=RateLimitMode.SPACED, interval_seconds=0.3, max_concurrent=3
    )
    lim = RateLimiter.from_config("p", cfg)
    send_times: list[float] = []

    async def worker(work_seconds: float):
        # Correct order: concurrency gate first, pacing immediately before
        # the simulated send.
        await lim.acquire_concurrency_slot()
        try:
            await lim.acquire()
            send_times.append(time.monotonic())
            await asyncio.sleep(work_seconds)
        finally:
            lim.release_concurrency_slot()

    # 3 slow requests occupy every concurrency slot; 3 more queue up behind
    # them and would (under the buggy ordering) all fire together once the
    # slow ones finish.
    tasks = [asyncio.create_task(worker(1.0 if i < 3 else 0.0)) for i in range(6)]
    await asyncio.gather(*tasks)

    send_times.sort()
    gaps = [send_times[i + 1] - send_times[i] for i in range(len(send_times) - 1)]
    for g in gaps:
        assert g >= 0.25, (
            f"gap {g:.3f}s < configured 0.3s interval -- requests burst "
            "through together after the concurrency bottleneck cleared"
        )


# ── Registry ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_register_and_stats():
    reg = LimiterRegistry()
    reg.register("a", RateLimitConfig(mode=RateLimitMode.SPACED, rpm=60))
    reg.register("b", RateLimitConfig(mode=RateLimitMode.BURST, rpm=120, capacity=5))
    stats = reg.all_stats()
    names = {s.provider for s in stats}
    assert names == {"a", "b"}
    a = reg.get("a")
    # Smoke: acquire works through the registry.
    await a.acquire()


def test_registry_get_missing_raises():
    reg = LimiterRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")
