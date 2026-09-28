"""Plugin layer: registries for tunnel, certificate and health providers."""

from .providers import register_builtin_providers, register_defaults
from .registry import (
    CertificateProvider,
    ProviderRegistry,
    TunnelProvider,
    certificates,
    health,
    register_certificate_provider,
    register_tunnel_provider,
    tunnels,
)

# Built-in providers are available as soon as the plugin package is imported.
# Custom providers can still be registered later via the registries above.
register_defaults()

__all__ = [
    "CertificateProvider",
    "ProviderRegistry",
    "TunnelProvider",
    "certificates",
    "health",
    "register_builtin_providers",
    "register_certificate_provider",
    "register_defaults",
    "register_tunnel_provider",
    "tunnels",
]
