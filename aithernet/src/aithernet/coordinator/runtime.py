"""The coordinator runtime: a thin, provider-agnostic reasoning facade.

``CoordinatorRuntime`` holds the configured provider and exposes :meth:`decide`. It is
the seam the node runtime depends on, and the unit tests inject a test-only provider
here. It contains no database or HTTP-framework code.
"""

from __future__ import annotations

from aithernet.config.settings import CoordinatorConfig
from aithernet.coordinator.contracts import (
    ACTIVE_CAPABILITIES,
    FUTURE_CAPABILITIES,
    CoordinatorConfigurationError,
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorStatus,
)
from aithernet.coordinator.providers import build_provider
from aithernet.coordinator.providers.base import CoordinatorProvider


class CoordinatorRuntime:
    """Owns the configured coordinator provider and mediates reasoning calls."""

    def __init__(self, config: CoordinatorConfig, provider: CoordinatorProvider | None) -> None:
        self.config = config
        self.provider = provider

    @classmethod
    def from_config(cls, config: CoordinatorConfig) -> CoordinatorRuntime:
        """Build a runtime, resolving the provider named in ``config``.

        Construction never fails: an unknown or unconfigured provider yields a runtime
        that reports itself as not configured and raises a clear error only if asked to
        reason.
        """
        return cls(config, build_provider(config))

    def is_configured(self) -> bool:
        """True when a known provider is selected and its required config is present."""
        return self.provider is not None and not self.provider.check_ready()

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        """Delegate reasoning to the configured provider.

        Raises :class:`CoordinatorConfigurationError` when no usable provider is
        configured, rather than fabricating a decision.
        """
        if self.provider is None:
            raise CoordinatorConfigurationError(
                f"No coordinator provider is configured for '{self.config.provider}'. "
                "Set a known provider and its required configuration."
            )
        return await self.provider.decide(payload)

    def status(self) -> CoordinatorStatus:
        """Return a secret-free configuration snapshot for the status endpoint.

        For CLI providers (e.g. ``gemini_cli``) this surfaces the executable and the cached
        CLI version (from the lightweight identity probe — never a paid inference). For HTTP
        providers it reports only whether base_url/api_key are present, never their values.
        """
        if self.provider is None:
            missing = [f"provider (unknown: '{self.config.provider}')"]
        else:
            missing = self.provider.check_ready()

        is_cli = self.provider is not None and hasattr(self.provider, "cli_version")
        cli_version = self.provider.cli_version() if is_cli else None
        return CoordinatorStatus(
            provider=self.config.provider,
            model=self.config.model,
            configured=self.is_configured(),
            missing_configuration=missing,
            executable=self.config.executable if is_cli else None,
            cli_version=cli_version,
            base_url_configured=bool(self.config.base_url),
            api_key_configured=bool(self.config.api_key),
            active_capabilities=list(ACTIVE_CAPABILITIES),
            future_capabilities=list(FUTURE_CAPABILITIES),
        )
