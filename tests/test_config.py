"""Tests for config loading: ENV expansion + pydantic validation."""
from __future__ import annotations

import textwrap

import pytest

from app.config import ConfigError, load_config
from app.models import AppConfig, RateLimitMode


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_load_minimal_valid(tmp_path):
    p = _write(
        tmp_path,
        """
        server: {host: 127.0.0.1, port: 9090}
        providers:
          - name: p1
            base_url: https://api.openai.com/v1
            api_key: sk-test
            models: [gpt-4o]
            rate_limit: {mode: spaced, rpm: 30}
        """,
    )
    cfg = load_config(p)
    assert isinstance(cfg, AppConfig)
    assert cfg.server.port == 9090
    assert cfg.providers[0].rate_limit.mode == RateLimitMode.SPACED
    # rpm=30 => 2s interval
    assert cfg.providers[0].rate_limit.effective_interval == 2.0


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("GATEWAY_TOKEN", "gw-secret")
    p = _write(
        tmp_path,
        """
        server: {auth_token: '${GATEWAY_TOKEN}'}
        providers:
          - name: p1
            base_url: https://api.openai.com/v1
            api_key: ${OPENAI_API_KEY}
            models: [gpt-4o]
            rate_limit: {mode: spaced, rpm: 60}
        """,
    )
    cfg = load_config(p)
    assert cfg.providers[0].api_key == "sk-from-env"
    assert cfg.server.auth_token == "gw-secret"


def test_env_var_with_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    p = _write(
        tmp_path,
        """
        server: {host: 0.0.0.0, port: 8080}
        providers:
          - name: p1
            base_url: https://api.openai.com/v1
            api_key: '${MISSING_VAR:-sk-fallback}'
            models: [gpt-4o]
            rate_limit: {mode: spaced, rpm: 60}
        """,
    )
    cfg = load_config(p)
    assert cfg.providers[0].api_key == "sk-fallback"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_spaced_requires_rpm_or_interval(tmp_path):
    p = _write(
        tmp_path,
        """
        server: {port: 8080}
        providers:
          - name: p1
            base_url: https://x
            api_key: k
            models: [m]
            rate_limit: {mode: spaced}
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_burst_requires_rpm(tmp_path):
    p = _write(
        tmp_path,
        """
        server: {port: 8080}
        providers:
          - name: p1
            base_url: https://x
            api_key: k
            models: [m]
            rate_limit: {mode: burst, capacity: 5}
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_duplicate_model_in_same_provider_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        server: {port: 8080}
        providers:
          - name: p1
            base_url: https://x
            api_key: k
            models: [m, m]
            rate_limit: {mode: spaced, rpm: 60}
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_no_providers_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        server: {port: 8080}
        providers: []
        """,
    )
    with pytest.raises(ConfigError):
        load_config(p)
