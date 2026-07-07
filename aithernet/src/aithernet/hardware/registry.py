"""The hardware discovery provider registry (Stage 14B, Part B).

Owns the set of configured discovery providers for one node. A test may inject an explicit fake
provider via :meth:`register` — production never invents devices. The registry only constructs
and looks up providers; the inventory service drives them on a bounded schedule.
"""

from __future__ import annotations

import contextlib

from aithernet.config.settings import HardwareConfig
from aithernet.hardware.contracts import HardwareDiscoveryBackend
from aithernet.hardware.discovery import build_provider


class HardwareDiscoveryRegistry:
    """Constructs + holds the enabled discovery providers for a node."""

    def __init__(self, config: HardwareConfig) -> None:
        self.config = config
        self._providers: dict[str, HardwareDiscoveryBackend] = {}
        self._required: dict[str, bool] = {}
        self._build()

    def _build(self) -> None:
        for provider_id, provider_cfg in (self.config.providers or {}).items():
            if not provider_cfg.enabled:
                continue
            provider = build_provider(provider_id, provider_cfg)
            if provider is None:
                continue
            self._providers[provider_id] = provider
            self._required[provider_id] = provider_cfg.required

    def register(self, provider: HardwareDiscoveryBackend, *, required: bool = False) -> None:
        """Inject a provider (used by tests to add an explicit fake discovery backend)."""
        self._providers[provider.backend_id] = provider
        self._required[provider.backend_id] = required

    def provider_ids(self) -> list[str]:
        return list(self._providers)

    def required_ids(self) -> list[str]:
        return [pid for pid, required in self._required.items() if required]

    def is_required(self, provider_id: str) -> bool:
        return self._required.get(provider_id, False)

    def get(self, provider_id: str) -> HardwareDiscoveryBackend | None:
        return self._providers.get(provider_id)

    def providers(self) -> list[HardwareDiscoveryBackend]:
        return list(self._providers.values())

    @property
    def has_providers(self) -> bool:
        return bool(self._providers)

    async def availability(self) -> dict[str, bool]:
        """Return ``{provider_id: available}`` — a provider error reads as unavailable."""
        out: dict[str, bool] = {}
        for provider_id, provider in self._providers.items():
            available = False
            with contextlib.suppress(Exception):
                available = bool(await provider.available())
            out[provider_id] = available
        return out
