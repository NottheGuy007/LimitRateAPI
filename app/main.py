"""FastAPI application: wire config → registry → limiters → routes."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import ConfigError, load_config
from .models import AppConfig
from .rate_limiter import LimiterRegistry
from .registry import GatewayState
from .routes import admin, chat, models_list

DEFAULT_CONFIG_PATH = "config.yaml"


def _config_path() -> str:
    return os.environ.get("KEEPALIVE_CONFIG", DEFAULT_CONFIG_PATH)


def _load_app_config() -> AppConfig:
    path = _config_path()
    try:
        return load_config(path)
    except ConfigError as e:
        # Fail fast with a clear message at startup.
        raise SystemExit(f"config error: {e}") from e


def _setup_state(app: FastAPI, cfg: AppConfig) -> None:
    """Build gateway state + limiters + http client on app.state.

    Factored out so tests can inject an in-memory config without touching the
    filesystem-based lifespan.
    """
    app.state.config = cfg
    app.state.gateway = GatewayState.from_config(cfg)
    limiters = LimiterRegistry()
    for p in cfg.providers:
        limiters.register(p.name, p.rate_limit)
    app.state.limiters = limiters
    app.state.http_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: AppConfig = _load_app_config()
    _setup_state(app, cfg)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the FastAPI app.

    Pass ``config`` to inject an in-memory AppConfig (used by tests); omit it
    to load from the YAML file at startup via the lifespan.
    """
    # If a config is injected we run setup eagerly and use a no-op lifespan so
    # TestClient doesn't re-load from disk.
    if config is not None:

        @asynccontextmanager
        async def _injected_lifespan(app: FastAPI):
            _setup_state(app, config)
            try:
                yield
            finally:
                await app.state.http_client.aclose()

        life = _injected_lifespan
    else:
        life = lifespan

    app = FastAPI(
        title="keepalive-api",
        description=(
            "OpenAI-compatible LLM gateway with per-provider outgoing rate "
            "shaping. Over-limit requests are buffered and sent at the "
            "configured cadence instead of being rejected."
        ),
        version="0.1.0",
        lifespan=life,
    )

    app.include_router(chat.router)
    app.include_router(models_list.router)
    app.include_router(admin.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ARG001
        # Keep upstream-style error envelopes consistent.
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "internal_error"}},
        )

    return app


app = create_app()


def main() -> None:
    """Entry point for `python -m app.main` / `uvicorn app.main:app`."""
    import uvicorn

    # Server host/port come from the config file; reload picks up code changes.
    cfg = _load_app_config()
    uvicorn.run(
        "app.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
