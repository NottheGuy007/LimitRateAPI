"""Provider registry + model-name routing."""
from __future__ import annotations

from dataclasses import dataclass

from .models import AppConfig, ProviderConfig


class ProviderNotFound(KeyError):
    """No provider serves the requested model."""


@dataclass
class GatewayState:
    """Holds the parsed config. Rebuilt on (future) config reload.

    Kept tiny and explicit so it's easy to swap for a version backed by a
    config store or Redis later.
    """

    providers: dict[str, ProviderConfig]  # name -> config
    model_index: dict[str, str]  # model_name -> provider name (first match)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "GatewayState":
        providers: dict[str, ProviderConfig] = {}
        model_index: dict[str, str] = {}
        for p in cfg.providers:
            if p.name in providers:
                raise ValueError(f"duplicate provider name: {p.name}")
            providers[p.name] = p
            for m in p.models:
                # First provider to declare a model wins (v1; future versions
                # could collect a list for load balancing).
                model_index.setdefault(m, p.name)
        return cls(providers=providers, model_index=model_index)

    def resolve(self, model: str) -> ProviderConfig:
        """Find the provider serving ``model``.

        Raises ProviderNotFound with a helpful message including the known
        models when the name doesn't match.
        """
        # Direct match first.
        provider_name = self.model_index.get(model)
        if provider_name is None:
            # Allow "<provider>/<model>" as an explicit override, useful when
            # two providers expose the same model name.
            if "/" in model:
                prov, _rest = model.split("/", 1)
                if prov in self.providers:
                    return self.providers[prov]
            known = ", ".join(sorted(self.model_index)) or "(none configured)"
            raise ProviderNotFound(
                f"no provider serves model '{model}'. Known models: {known}"
            )
        return self.providers[provider_name]

    def all_models(self) -> list[str]:
        return sorted(self.model_index.keys())
