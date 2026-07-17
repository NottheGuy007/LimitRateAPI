"""Rate shaper: smooth outgoing requests to a per-provider rate.

Design (the core of keepalive-api):
- We *shape* traffic rather than *police* it. Over-limit requests are held
  until a slot is available, not rejected. This is the "buffer then send"
  behavior the project is built for.
- Both modes (SPACED strict interval, BURST token bucket) are expressed as a
  single token-bucket primitive that allows its token count to go *negative*.
  A negative balance represents "requests waiting in line"; the wait time is
  exactly |balance| / refill_rate. This gives strict FIFO ordering for free
  and keeps inter-send timing mathematically correct under any concurrency,
  without maintaining an explicit queue.

Acquisition protocol (the trick that makes concurrent callers FIFO + non-
serialized):

    async with bucket.at_acquire(max_wait):
        # we hold a token; the next send "now" is guaranteed >= interval
        # after the previous send for SPACED, or respects the bucket for BURST
        ... do the upstream call ...

Internally, ``acquire()`` reserves a slot (advancing the bucket's notion of
"the next allowed send time") under a short-held lock, then RELEASES the lock
and sleeps until that slot. Different concurrent callers therefore don't
serialize on each other's waits — only on the tiny reservation critical section.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from .models import RateLimitConfig, RateLimitMode


class RateLimitTimeoutError(Exception):
    """Raised when a request waited longer than ``max_wait_seconds``.

    Mapped to HTTP 503 by the route layer (with a Retry-After hint).
    """

    def __init__(self, provider: str, waited: float, retry_after: float):
        self.provider = provider
        self.waited = waited
        self.retry_after = retry_after
        super().__init__(
            f"provider '{provider}' queue full: waited {waited:.2f}s, "
            f"retry after {retry_after:.2f}s"
        )


@dataclass
class LimiterStats:
    """Live snapshot of a provider's limiter, for /admin/stats."""

    provider: str
    mode: RateLimitMode
    interval_seconds: float
    capacity: int
    tokens: float  # current balance (can be negative => queued debt)
    waiting: int  # callers currently sleeping between reserve and send
    last_send_time: Optional[float]
    max_concurrent: Optional[int] = None  # configured concurrency cap, if any
    in_flight: int = 0  # requests currently holding a concurrency slot


@dataclass
class RateLimiter:
    """Async rate shaper for one provider.

    The bucket state:
        tokens   — available tokens. Decremented by 1 on each acquire.
                   Allowed to go negative; the debt is the queue depth.
        last_ts  — timestamp used to refill tokens (lazy refill on acquire).

    Refill rate ``R`` (tokens/second) = rpm / 60.

    For SPACED (capacity=1): after acquire, tokens becomes <= 0 every time a
    request fires back-to-back, so the next caller must wait exactly 1/R
    seconds. Net effect: strictly uniform sending at 1/R spacing.

    For BURST (capacity>1): tokens is clamped to [−inf, capacity]; an initial
    burst drains it to 0, then refills at R tokens/s, smoothing subsequent
    sends.
    """

    provider: str
    config: RateLimitConfig
    rate: float  # tokens per second = rpm / 60
    capacity: float
    tokens: float = 0.0
    last_ts: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _waiting: int = 0  # number of callers past reserve, not yet sent
    _last_send_time: Optional[float] = None
    # Concurrency cap: separate from send-rate pacing. rate/tokens above
    # control how often a NEW request may start; this controls how many
    # requests are allowed to be simultaneously un-finished (send -> fully
    # streamed back). None means uncapped (old behavior).
    _semaphore: Optional[asyncio.Semaphore] = field(
        default=None, init=False, repr=False
    )
    _in_flight: int = field(default=0, init=False, repr=False)

    # ── construction ───────────────────────────────────────────────────────
    @classmethod
    def from_config(cls, provider: str, cfg: RateLimitConfig) -> "RateLimiter":
        if cfg.mode == RateLimitMode.SPACED:
            # SPACED == token bucket with capacity 1. Tokens start full so the
            # very first request fires immediately.
            rate = 1.0 / cfg.effective_interval  # tokens/s
            lim = cls(
                provider=provider,
                config=cfg,
                rate=rate,
                capacity=1.0,
                tokens=1.0,
            )
        else:  # BURST
            rate = (cfg.rpm or 0.0) / 60.0
            lim = cls(
                provider=provider,
                config=cfg,
                rate=rate,
                capacity=float(cfg.capacity),
                tokens=float(cfg.capacity),
            )
        if cfg.max_concurrent is not None:
            lim._semaphore = asyncio.Semaphore(cfg.max_concurrent)
        return lim

    # ── core algorithm ─────────────────────────────────────────────────────
    async def acquire(self, max_wait: Optional[float] = None) -> None:
        """Reserve a send slot, waiting if necessary.

        Strategy (FIFO under concurrency):
        1. Hold the lock briefly to compute THIS caller's reserved send time
           and advance the bucket's "next available" bookkeeping.
        2. Release the lock, then sleep until the reserved time.
        Because step 1 happens under a lock in arrival order, callers get
        strictly increasing reserved times → strict FIFO.
        """
        max_wait = max_wait if max_wait is not None else self.config.max_wait_seconds
        arrival = time.monotonic()

        # ── critical section: reserve a slot ───────────────────────────────
        async with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            # Reserve this request: decrement first (we owe a token).
            self.tokens -= 1.0
            if self.tokens >= 0.0:
                # Bucket had enough; send now.
                wait = 0.0
            else:
                # In debt: must wait |debt| / R for enough refill to cover us.
                wait = (-self.tokens) / self.rate if self.rate > 0 else float("inf")
            # Clamp to capacity so a long idle period doesn't bank infinite
            # burst budget beyond what's configured.
            self.tokens = min(self.tokens, self.capacity)
            self._waiting += 1

        # ── outside the lock: enforce max_wait, then sleep ─────────────────
        try:
            if max_wait is not None and wait > max_wait:
                # Undo our reservation since we're giving up. We re-take the
                # lock to keep the bucket consistent; the freed slot lets a
                # later caller fire sooner.
                await self._release_reservation()
                waited = time.monotonic() - arrival
                raise RateLimitTimeoutError(
                    provider=self.provider,
                    waited=waited,
                    retry_after=wait - max_wait,
                )

            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_time = time.monotonic()
        finally:
            self._waiting -= 1

    # ── concurrency cap (separate from send-rate pacing above) ────────────
    async def acquire_concurrency_slot(self) -> None:
        """Block until an in-flight slot is free, if max_concurrent is set.

        No-op (returns immediately) if no concurrency cap is configured.
        Callers must pair this with exactly one ``release_concurrency_slot()``
        call once the request (including any streamed body) is fully done —
        use a try/finally or the ``concurrency_slot()`` context manager to
        guarantee that even on error.
        """
        if self._semaphore is not None:
            await self._semaphore.acquire()
            self._in_flight += 1

    def release_concurrency_slot(self) -> None:
        """Release a slot acquired via ``acquire_concurrency_slot()``.

        No-op if no concurrency cap is configured. Safe to call even if the
        matching acquire failed partway, as long as it's only called once per
        successful acquire.
        """
        if self._semaphore is not None:
            self._in_flight -= 1
            self._semaphore.release()

    @asynccontextmanager
    async def concurrency_slot(self):
        """Async context manager wrapping acquire/release for simple
        (non-streaming) callers. Streaming callers (the chat routes) manage
        acquire/release manually so the slot spans the full streamed
        response, not just until headers arrive."""
        await self.acquire_concurrency_slot()
        try:
            yield
        finally:
            self.release_concurrency_slot()

    async def _release_reservation(self) -> None:
        """Return a reserved token to the bucket (used on timeout/give-up)."""
        async with self._lock:
            # Putting a token back can only help (or be a no-op if idle time
            # already refilled past capacity).
            self.tokens = min(self.tokens + 1.0, self.capacity)

    def _refill_locked(self, now: float) -> None:
        """Lazy token refill. Call only while holding ``_lock``."""
        elapsed = now - self.last_ts
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_ts = now

    # ── observability ──────────────────────────────────────────────────────
    def stats(self) -> LimiterStats:
        # Read tokens under the lock for a consistent snapshot.
        # (For simplicity we peek without the lock; a slightly-stale value is
        # fine for diagnostics.)
        interval = (
            self.config.effective_interval
            if self.config.mode == RateLimitMode.SPACED
            else (60.0 / (self.config.rpm or 1.0))
        )
        return LimiterStats(
            provider=self.provider,
            mode=self.config.mode,
            interval_seconds=interval,
            capacity=int(self.capacity),
            tokens=self.tokens,
            waiting=self._waiting,
            last_send_time=self._last_send_time,
            max_concurrent=self.config.max_concurrent,
            in_flight=self._in_flight,
        )


class LimiterRegistry:
    """Holds one RateLimiter per provider name."""

    def __init__(self) -> None:
        self._limiters: dict[str, RateLimiter] = {}

    def register(self, provider: str, cfg: RateLimitConfig) -> RateLimiter:
        lim = RateLimiter.from_config(provider, cfg)
        self._limiters[provider] = lim
        return lim

    def get(self, provider: str) -> RateLimiter:
        try:
            return self._limiters[provider]
        except KeyError:
            raise KeyError(f"no limiter registered for provider '{provider}'")

    def all_stats(self) -> list[LimiterStats]:
        return [lim.stats() for lim in self._limiters.values()]
