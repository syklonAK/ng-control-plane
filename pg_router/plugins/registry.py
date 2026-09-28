"""Plugin registry: extensibility point for providers.

Every variable part of the system (tunnel software, certificate source,
health probe method) is an adapter behind a registry. New providers are
registered without touching the routing core.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config.schema import Certificate, Tunnel
from ..config.schema import TunnelEndpoint

@runtime_checkable
class TunnelProvider(Protocol):
    """How the routing engine represents and reaches a tunnel.

    The provider never proxies traffic and never runs the tunnel software
    itself. It only answers "where does Nginx connect to reach the far side
    of this tunnel?".
    """

    name: str

    def endpoint(self, tunnel: Tunnel) -> TunnelEndpoint: ...

    def health_endpoint(self, tunnel: Tunnel) -> TunnelEndpoint | None:
        """Where to probe to learn whether the tunnel is up."""
        ...

    def describe(self, tunnel: Tunnel) -> dict:
        """Provider-specific metadata for status output."""
        ...


@runtime_checkable
class CertificateProvider(Protocol):
    """Resolves certificate material: existing files, ACME or custom."""

    name: str

    def resolve(self, certificate: Certificate) -> tuple[str, str]:
        """Return (chain_path, key_path), raising on failure."""
        ...


class ProviderRegistry:
    """Type-scoped registry of providers."""

    def __init__(self) -> None:
        self._providers: dict[str, object] = {}

    def register(self, provider: object) -> None:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("Provider must define a non-empty string 'name'")
        self._providers[name] = provider

    def get(self, name: str) -> object:
        if name not in self._providers:
            raise KeyError(
                f"No provider registered for {name!r}. Known: {sorted(self._providers)}"
            )
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)


tunnels = ProviderRegistry()
certificates = ProviderRegistry()
health = ProviderRegistry()


def register_tunnel_provider(provider: TunnelProvider) -> None:
    tunnels.register(provider)


def register_certificate_provider(provider: CertificateProvider) -> None:
    certificates.register(provider)
