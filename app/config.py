"""Configuration loading: YAML file with ${ENV_VAR} expansion + pydantic validation."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig

# Matches ${VAR} or ${VAR:-default} anywhere in a string value.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}")


class ConfigError(ValueError):
    """Raised when the config file is missing, malformed, or invalid."""


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} in strings.

    Lets users keep secrets out of the config file. A referenced var that is
    unset and has no default becomes an empty string (and pydantic will then
    reject empty api_keys where required).
    """
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path) -> AppConfig:
    """Load and validate an AppConfig from a YAML file."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"top-level of {p} must be a mapping/object")

    expanded = _expand_env(raw)
    try:
        return AppConfig.model_validate(expanded)
    except Exception as e:
        raise ConfigError(f"invalid config in {p}: {e}") from e
