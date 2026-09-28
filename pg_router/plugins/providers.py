"""Built-in providers.

Tunnel providers here do NOT implement GOST, SSH, WireGuard or any other
tunnel software. They only answer "where does Nginx connect?":

* ``reverse``/``direct``/``gost``/``ssh``/``wireguard``/``tcp-relay``/
  ``custom`` all resolve to an address that the deployment is responsible for
  making reachable; the routing engine stays implementation-independent.
* ``custom`` additionally honours ``options.endpoint_host`` /
  ``options.endpoint_port`` / ``options.endpoint_socket`` so an out-of-band
  tunnel can be described without changing the core.
"""

from __future__ import annotations

from typing import Optional

from ..config.schema import Tunnel, TunnelEndpoint
from ..utils.security import validate_filesystem_path
from .registry import (
    ProviderRegistry,
    certificates,
    register_tunnel_provider,
    tunnels,
)


class BaseTunnelProvider:
    """Direction-based endpoint resolution shared by most providers."""

    name = "base"

    def endpoint(self, tunnel: Tunnel) -> TunnelEndpoint:
        return tunnel.endpoint()

    def health_endpoint(self, tunnel: Tunnel) -> Optional[TunnelEndpoint]:
        # Probing the address Nginx would use is the most honest signal.
        return tunnel.endpoint()

    def describe(self, tunnel: Tunnel) -> dict:
        endpoint = self.endpoint(tunnel)
        return {
            "provider": self.name,
            "mode": tunnel.mode,
            "endpoint": f"{endpoint.host}:{endpoint.port}",
            "target": (
                f"{tunnel.target_host}:{tunnel.target_port}"
                if tunnel.target_host and tunnel.target_port
                else None
            ),
            "remote_node": tunnel.remote_node,
        }


class ReverseTunnelProvider(BaseTunnelProvider):
    name = "reverse"


class DirectTunnelProvider(BaseTunnelProvider):
    name = "direct"


class GostProvider(BaseTunnelProvider):
    name = "gost"


class SshProvider(BaseTunnelProvider):
    name = "ssh"


class WireguardProvider(BaseTunnelProvider):
    name = "wireguard"


class TcpRelayProvider(BaseTunnelProvider):
    name = "tcp-relay"


class UnixSocketTunnelProvider(BaseTunnelProvider):
    """Tunnel reached through a unix domain socket."""

    name = "unix-socket"

    def endpoint(self, tunnel: Tunnel) -> TunnelEndpoint:
        socket = tunnel.options.get("endpoint_socket") or tunnel.options.get("socket")
        if not socket:
            raise ValueError("unix-socket tunnel requires options.endpoint_socket")
        validate_filesystem_path(str(socket))
        return TunnelEndpoint("unix", 0, socket=str(socket))

    def health_endpoint(self, tunnel: Tunnel) -> Optional[TunnelEndpoint]:
        return None  # socket reachability is checked by the health manager


class CustomTunnelProvider(BaseTunnelProvider):
    """Out-of-band tunnel described entirely by options."""

    name = "custom"

    def endpoint(self, tunnel: Tunnel) -> TunnelEndpoint:
        host = tunnel.options.get("endpoint_host")
        port = tunnel.options.get("endpoint_port")
        socket = tunnel.options.get("endpoint_socket")
        if socket:
            validate_filesystem_path(str(socket))
            return TunnelEndpoint("unix", 0, socket=str(socket))
        if host and port:
            return TunnelEndpoint(str(host), int(port))
        # fall back to the declared direction
        return super().endpoint(tunnel)


# --- certificate providers -------------------------------------------------


class ExistingCertificateProvider:
    """Use certificate files already present on disk."""

    name = "existing"

    def resolve(self, certificate) -> tuple[str, str]:
        import os

        if not certificate.chain or not certificate.key:
            raise ValueError(f"certificate {certificate.id!r} has no chain/key paths")
        for path in (certificate.chain, certificate.key):
            if not os.path.exists(path):
                raise FileNotFoundError(f"certificate {certificate.id!r}: missing file {path}")
        return certificate.chain, certificate.key


class AcmeCertificateProvider:
    """Issue a certificate through an ACME client (certbot by default).

    The actual issuance is delegated to the external client; this provider
    only orchestrates it and reports the resulting paths.
    """

    name = "acme"

    def __init__(self, issuer=None) -> None:
        # issuer is injected for testability; production uses certbot
        self._issuer = issuer

    def resolve(self, certificate) -> tuple[str, str]:
        if not certificate.domains:
            raise ValueError(f"ACME certificate {certificate.id!r} requires 'domains'")
        if self._issuer is not None:
            chain, key = self._issuer(certificate)
            return chain, key
        from ..nginx.manager import NginxManager

        manager = NginxManager()
        manager.stop()
        try:
            from ..utils.system import run

            args = ["certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos"]
            if certificate.email:
                args += ["-m", certificate.email]
            else:
                args.append("--register-unsafely-without-email")
            if certificate.acme_directory:
                args += ["--server", certificate.acme_directory]
            for domain in certificate.domains:
                args += ["-d", domain]
            run(args, timeout=300)
        finally:
            manager.start()
        primary = certificate.domains[0]
        return (
            f"/etc/letsencrypt/live/{primary}/fullchain.pem",
            f"/etc/letsencrypt/live/{primary}/privkey.pem",
        )


class CustomCertificateProvider:
    """Certificate material produced by an external hook (never shell-interpolated)."""

    name = "custom"

    def resolve(self, certificate) -> tuple[str, str]:
        if not certificate.chain or not certificate.key:
            raise ValueError(f"certificate {certificate.id!r} must define chain/key paths")
        return certificate.chain, certificate.key


def register_builtin_providers(
    tunnel_registry: ProviderRegistry,
    certificate_registry: ProviderRegistry,
) -> None:
    """Register all built-in providers (idempotent)."""
    for provider in (
        ReverseTunnelProvider(),
        DirectTunnelProvider(),
        GostProvider(),
        SshProvider(),
        WireguardProvider(),
        TcpRelayProvider(),
        UnixSocketTunnelProvider(),
        CustomTunnelProvider(),
    ):
        tunnel_registry.register(provider)
    for provider in (
        ExistingCertificateProvider(),
        AcmeCertificateProvider(),
        CustomCertificateProvider(),
    ):
        certificate_registry.register(provider)


def register_defaults() -> None:
    """Register all built-in providers against the module registries."""
    register_builtin_providers(tunnels, certificates)


def register_builtin_tunnel_provider(provider) -> None:
    register_tunnel_provider(provider)
