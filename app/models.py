"""Data models for keepalive-api configuration."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class RateLimitMode(str, Enum):
    """How a provider rate-limits outgoing requests.

    - SPACED: strict, uniform interval between sends. The most literal reading of
      "split RPM into 'send once every N seconds'". Implemented as a token bucket
      with capacity 1, so consecutive sends are always at least ``interval`` apart.
    - BURST: classic token bucket with a refill rate derived from RPM and a
      configurable burst ``capacity``. Allows a short burst of ``capacity``
      back-to-back requests, then smooths out to the RPM rate.
    """

    SPACED = "spaced"
    BURST = "burst"


class RateLimitConfig(BaseModel):
    """Per-provider outgoing rate shaping.

    The goal of keepalive-api is *shaping* (smooth the traffic) rather than
    *policing* (reject on overflow) — so by default over-limit requests are held
    until a slot frees up, not rejected.
    """

    mode: RateLimitMode = RateLimitMode.SPACED

    rpm: Optional[float] = Field(
        default=None,
        description="Requests-per-minute budget. Converted to a per-second "
        "refill rate of R = rpm/60. e.g. rpm=60 -> one send every 1s.",
    )
    interval_seconds: Optional[float] = Field(
        default=None,
        description="Explicit minimum interval between sends, in seconds. "
        "For SPACED mode only. If both rpm and interval_seconds are set, the "
        "stricter (larger) interval wins.",
    )

    capacity: int = Field(
        default=1,
        ge=1,
        description="Token-bucket burst capacity (BURST mode only). The number "
        "of requests that can fire back-to-back before smoothing kicks in.",
    )
    max_wait_seconds: Optional[float] = Field(
        default=None,
        description="How long an over-limit request is willing to wait in the "
        "queue before giving up. null = wait forever (default, most faithful to "
        "'buffer then send'). A finite value returns HTTP 503 on timeout.",
    )

    max_concurrent: Optional[int] = Field(
        default=None,
        ge=1,
        description="Max number of requests to this provider allowed in flight "
        "at once, from send until the upstream response is fully streamed "
        "back. Independent from rpm/interval: rpm paces how often a NEW "
        "request may START, but says nothing about how many earlier requests "
        "are still running when it does. For slow upstreams (tens of seconds "
        "per call), many paced-but-not-yet-finished requests can pile up "
        "concurrently and trip the upstream's own concurrency-sensitive rate "
        "limiting even while staying within the configured rpm. "
        "null = no concurrency cap (default; preserves old behavior).",
    )

    retry_upstream_429: bool = Field(
        default=True,
        description="If the upstream itself returns HTTP 429 (its own rate "
        "limit, not ours), transparently retry instead of forwarding the 429 "
        "to the client. Honors the upstream's Retry-After header when "
        "present; falls back to exponential backoff otherwise. This closes "
        "the gap where local pacing alone doesn't stop the upstream from "
        "rejecting a request that arrives while it's independently busy "
        "(e.g. still finishing other in-flight calls).",
    )
    max_upstream_retries: int = Field(
        default=5,
        ge=0,
        description="Max retry attempts on an upstream 429 before giving up "
        "and forwarding the 429 to the client as-is. Only used when "
        "retry_upstream_429 is true.",
    )
    upstream_retry_backoff_seconds: float = Field(
        default=1.0,
        gt=0,
        description="Base backoff (seconds) used when the upstream 429 has no "
        "Retry-After header. Doubles each retry attempt (capped at 30s), "
        "with a small jitter to avoid retry storms across concurrent "
        "requests hitting the same limit at once.",
    )

    @model_validator(mode="after")
    def _check_mode_params(self) -> "RateLimitConfig":
        if self.mode == RateLimitMode.SPACED:
            # Need at least one way to derive the interval.
            if self.rpm is None and self.interval_seconds is None:
                raise ValueError(
                    "spaced mode requires at least one of 'rpm' or "
                    "'interval_seconds'"
                )
        else:  # BURST
            if self.rpm is None:
                raise ValueError("burst mode requires 'rpm'")
            if self.capacity < 1:
                raise ValueError("burst 'capacity' must be >= 1")
        return self

    @property
    def effective_interval(self) -> float:
        """Minimum seconds between two consecutive sends (SPACED mode).

        interval = max(rpm-derived interval, explicit interval_seconds).
        The max ensures the stricter limit wins when BOTH are configured;
        when only one is set, that one is used directly.
        """
        if self.mode == RateLimitMode.BURST:
            # Not meaningful for burst (capacity>1 lets them bunch up), but
            # expose the per-token refill interval for diagnostics.
            return 60.0 / (self.rpm or 1.0)
        candidates: list[float] = []
        if self.rpm:
            candidates.append(60.0 / self.rpm)
        if self.interval_seconds:
            candidates.append(self.interval_seconds)
        # Stricter = larger interval (slower send cadence), so take the max of
        # whichever candidates are actually configured.
        return max(candidates) if candidates else float("inf")

    @field_validator("rpm")
    @classmethod
    def _positive_rpm(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("rpm must be > 0")
        return v

    @field_validator("interval_seconds")
    @classmethod
    def _positive_interval(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("interval_seconds must be > 0")
        return v


class ProviderConfig(BaseModel):
    """One upstream OpenAI-compatible endpoint plus its rate shaper."""

    name: str = Field(..., description="Stable provider id used in routing/stats.")
    base_url: str
    api_key: str
    models: list[str] = Field(
        default_factory=list,
        description="Model names exposed by this provider. Requests whose "
        "'model' field matches one of these route here. A model name unique to "
        "one provider routes directly; if a model appears on multiple providers, "
        "load-balancing/fallback is possible (not in v1).",
    )
    rate_limit: RateLimitConfig = Field(default_factory=lambda: RateLimitConfig(
        mode=RateLimitMode.SPACED, rpm=60
    ))
    timeout_seconds: float = Field(
        default=300.0, ge=1.0, description="Upstream request timeout."
    )

    model_config = {"extra": "forbid"}


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: Optional[str] = Field(
        default=None,
        description="If set, clients must send Authorization: Bearer <token>. "
        "If null, no gateway-level auth (the upstream api_key still applies).",
    )

    model_config = {"extra": "forbid"}


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: list[ProviderConfig] = Field(..., min_length=1)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_unique_models_per_name(self) -> "AppConfig":
        # Catch duplicate (provider, model) pairs early; different providers
        # MAY expose the same model name in v1 (first match wins), but the same
        # provider listing a model twice is almost certainly a typo.
        seen: set[tuple[str, str]] = set()
        for p in self.providers:
            for m in p.models:
                key = (p.name, m)
                if key in seen:
                    raise ValueError(
                        f"provider '{p.name}' lists model '{m}' more than once"
                    )
                seen.add(key)
        return self
